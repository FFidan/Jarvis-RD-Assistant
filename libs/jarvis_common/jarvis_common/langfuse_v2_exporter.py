"""Bounded OpenTelemetry export to the Langfuse 2 batch-ingestion API."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

import httpx
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

from jarvis_common.maintenance import outbound_quarantine_active
from jarvis_common.pinned_transport import LANGFUSE_EXPORT_POLICY, PinnedBlockingTransport

_OBSERVATION_TYPE = "langfuse.observation.type"
_OBSERVATION_INPUT = "langfuse.observation.input"
_OBSERVATION_OUTPUT = "langfuse.observation.output"
_OBSERVATION_MODEL = "langfuse.observation.model.name"
_OBSERVATION_STATUS = "langfuse.observation.status_message"
_MAX_SPANS_PER_REQUEST = 50


def _timestamp(nanoseconds: int | None) -> datetime:
    """Convert an OpenTelemetry nanosecond timestamp to an aware datetime."""
    value = nanoseconds or 0
    return datetime.fromtimestamp(value / 1_000_000_000, tz=UTC)


def _iso_timestamp(nanoseconds: int | None) -> str:
    """Return the legacy ingestion timestamp representation."""
    return _timestamp(nanoseconds).isoformat().replace("+00:00", "Z")


def _decode_json_attribute(value: object) -> object | None:
    """Decode Langfuse's JSON-string span attributes without guessing types."""
    if value is None:
        return None
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return value


def _event_id(kind: str, trace_id: str, span_id: str = "") -> str:
    """Build a deterministic UUID accepted by the legacy ingestion endpoint."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"jarvis:{kind}:{trace_id}:{span_id}"))


def _span_payload(span: ReadableSpan) -> tuple[dict[str, object], dict[str, object]] | None:
    """Translate one Langfuse SDK span into trace and observation events."""
    attributes: Mapping[str, object] = span.attributes or {}
    observation_type = attributes.get(_OBSERVATION_TYPE)
    if not isinstance(observation_type, str):
        return None

    context = span.context
    if context is None or not context.is_valid:
        return None
    trace_id = f"{context.trace_id:032x}"
    span_id = f"{context.span_id:016x}"
    parent_id = (
        f"{span.parent.span_id:016x}" if span.parent is not None and span.parent.is_valid else None
    )
    started = _timestamp(span.start_time)
    ended = _timestamp(span.end_time)
    event_time = _iso_timestamp(span.start_time)

    trace_event: dict[str, object] = {
        "id": _event_id("trace", trace_id),
        "timestamp": event_time,
        "type": "trace-create",
        "body": {
            "id": trace_id,
            "timestamp": started.isoformat(),
            "name": span.name,
        },
    }
    body: dict[str, object] = {
        "id": span_id,
        "traceId": trace_id,
        "name": span.name,
        "startTime": started.isoformat(),
        "endTime": ended.isoformat(),
    }
    if parent_id is not None:
        body["parentObservationId"] = parent_id
    input_value = _decode_json_attribute(attributes.get(_OBSERVATION_INPUT))
    output_value = _decode_json_attribute(attributes.get(_OBSERVATION_OUTPUT))
    if input_value is not None:
        body["input"] = input_value
    if output_value is not None:
        body["output"] = output_value
    status_message = attributes.get(_OBSERVATION_STATUS)
    if isinstance(status_message, str):
        body["statusMessage"] = status_message

    is_generation = observation_type in {"generation", "embedding"}
    if is_generation:
        model = attributes.get(_OBSERVATION_MODEL)
        if isinstance(model, str):
            body["model"] = model
    event_type = "generation-create" if is_generation else "span-create"
    observation_event = {
        "id": _event_id(event_type, trace_id, span_id),
        "timestamp": event_time,
        "type": event_type,
        "body": body,
    }
    return trace_event, observation_event


def _remove_dangling_parent_ids(
    translated: Sequence[tuple[dict[str, object], dict[str, object]]],
) -> None:
    """Remove parent references to generic spans omitted from the batch."""
    observation_ids = {
        body.get("id") for _, event in translated if isinstance((body := event.get("body")), dict)
    }
    for _, event in translated:
        body = event.get("body")
        if not isinstance(body, dict):
            continue
        parent_id = body.get("parentObservationId")
        if parent_id is not None and parent_id not in observation_ids:
            body.pop("parentObservationId")


class LangfuseV2SpanExporter(SpanExporter):
    """Export Langfuse SDK spans through the self-hosted v2 ingestion route.

    Parameters
    ----------
    base_url : str
        Base URL of the self-hosted Langfuse server.
    public_key : str
        Project public key used for HTTP Basic authentication.
    secret_key : str
        Project secret key used for HTTP Basic authentication.
    timeout_seconds : float
        Per-request network timeout. Export runs on the SDK batch thread and
        never on the product request path.
    """

    def __init__(
        self,
        *,
        base_url: str,
        public_key: str,
        secret_key: str,
        timeout_seconds: float = 5.0,
    ) -> None:
        """Initialize the bounded self-hosted ingestion client.

        Parameters
        ----------
        base_url : str
            Base URL of the self-hosted Langfuse server.
        public_key : str
            Project public key used for HTTP Basic authentication.
        secret_key : str
            Project secret key used for HTTP Basic authentication.
        timeout_seconds : float, optional
            Per-request network timeout in seconds.

        """
        self._endpoint = f"{base_url.rstrip('/')}/api/public/ingestion"
        # Spans carry model inputs and outputs, so this export is a credential-
        # bearing sink like every other outbound client: it resolves through the
        # pinned transport and never inherits a proxy from the environment.
        self._client = httpx.Client(
            transport=PinnedBlockingTransport(LANGFUSE_EXPORT_POLICY),
            auth=(public_key, secret_key),
            timeout=timeout_seconds,
            trust_env=False,
            headers={"Content-Type": "application/json"},
        )

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        """Export one bounded batch of Langfuse observation spans.

        Parameters
        ----------
        spans : Sequence[ReadableSpan]
            Completed OpenTelemetry spans offered by the batch processor.
            Generic spans and spans blocked by outbound quarantine are not
            transmitted.

        Returns
        -------
        SpanExportResult
            ``SUCCESS`` when the eligible observations were accepted or no
            eligible observations remained, otherwise ``FAILURE``.
        """
        if outbound_quarantine_active():
            return SpanExportResult.SUCCESS
        translated = [payload for span in spans if (payload := _span_payload(span)) is not None]
        if not translated:
            return SpanExportResult.SUCCESS
        _remove_dangling_parent_ids(translated)
        events: list[dict[str, object]] = []
        seen_traces: set[str] = set()
        for trace_event, observation_event in translated:
            trace_body = trace_event["body"]
            trace_id = (
                str(trace_body.get("id")) if isinstance(trace_body, dict) else str(trace_body)
            )
            if trace_id not in seen_traces:
                events.append(trace_event)
                seen_traces.add(trace_id)
            events.append(observation_event)
        try:
            for start in range(0, len(events), _MAX_SPANS_PER_REQUEST * 2):
                response = self._client.post(
                    self._endpoint,
                    json={"batch": events[start : start + _MAX_SPANS_PER_REQUEST * 2]},
                )
                if response.status_code not in {200, 207}:
                    return SpanExportResult.FAILURE
                if response.status_code == 207:
                    result = response.json()
                    if isinstance(result, dict) and result.get("errors"):
                        return SpanExportResult.FAILURE
        except (httpx.HTTPError, ValueError):
            return SpanExportResult.FAILURE
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        """Close the private HTTP client used only by the exporter thread."""
        self._client.close()

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        """Report that no exporter-local buffer remains to flush.

        Parameters
        ----------
        timeout_millis : int, default=30000
            Compatibility timeout supplied by the OpenTelemetry processor.
            The value is unused because :meth:`export` completes synchronously.

        Returns
        -------
        bool
            Always ``True`` because the exporter retains no pending batch.
        """
        del timeout_millis
        return True


__all__ = ["LangfuseV2SpanExporter"]
