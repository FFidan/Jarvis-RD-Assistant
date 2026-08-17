"""Bounded, vendor-neutral trace context and service telemetry helpers."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Iterator, Mapping, MutableMapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from opentelemetry import context, trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.trace import SpanKind
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from jarvis_common.logging_config import correlation_id_var

_PROPAGATOR = TraceContextTextMapPropagator()
_TASK_CONTEXT_KEY = "_jarvis_telemetry"
_METRICS_LOCK = threading.Lock()


@dataclass(slots=True)
class _ServiceMetrics:
    requests: int = 0
    request_errors: int = 0
    request_duration_ms: int = 0
    workers: int = 0
    worker_errors: int = 0
    worker_duration_ms: int = 0


_METRICS: dict[str, _ServiceMetrics] = {}
_EXPORT_ENDPOINTS: set[str] = set()
_TELEMETRY_SCOPE = "jarvis_common.telemetry"


def _service_metrics(service: str) -> _ServiceMetrics:
    with _METRICS_LOCK:
        return _METRICS.setdefault(service, _ServiceMetrics())


class _JarvisSpanExporter(SpanExporter):
    """Export only spans created by the bounded JARVIS telemetry scope."""

    def __init__(self, delegate: SpanExporter) -> None:
        self._delegate = delegate

    def export(self, spans: list[ReadableSpan]) -> SpanExportResult:
        accepted = [
            span
            for span in spans
            if getattr(getattr(span, "instrumentation_scope", None), "name", None)
            == _TELEMETRY_SCOPE
        ]
        return self._delegate.export(accepted) if accepted else SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        self._delegate.shutdown()

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return self._delegate.force_flush(timeout_millis=timeout_millis)


def _valid_context(headers: Mapping[str, str]) -> context.Context:
    extracted = _PROPAGATOR.extract(headers)
    if trace.get_current_span(extracted).get_span_context().is_valid:
        return extracted
    return context.Context()


def trace_headers() -> dict[str, str]:
    """Return the active valid W3C headers, or an empty mapping."""
    carrier: dict[str, str] = {}
    if trace.get_current_span().get_span_context().is_valid:
        _PROPAGATOR.inject(carrier)
    return carrier


def trace_id() -> str | None:
    """Return the active trace ID without exposing span metadata."""
    span_context = trace.get_current_span().get_span_context()
    return f"{span_context.trace_id:032x}" if span_context.is_valid else None


def correlation_id() -> str | None:
    """Return the active stable correlation identifier."""
    value = correlation_id_var.get()
    return str(value) if value is not None else None


@contextmanager
def request_span(
    *,
    headers: Mapping[str, str],
    service: str,
    method: str,
) -> Iterator[None]:
    """Activate a server span, replacing malformed caller trace state."""
    tracer = trace.get_tracer(_TELEMETRY_SCOPE)
    with tracer.start_as_current_span(
        "http.request",
        context=_valid_context(headers),
        kind=SpanKind.SERVER,
        attributes={"service": service, "http.request.method": method},
    ):
        yield


@contextmanager
def restored_span(
    *,
    carrier: Mapping[str, str],
    service: str,
    name: str,
    kind: SpanKind,
) -> Iterator[None]:
    """Restore queued W3C context and make one bounded processing span."""
    tracer = trace.get_tracer(_TELEMETRY_SCOPE)
    with tracer.start_as_current_span(
        name,
        context=_valid_context(carrier),
        kind=kind,
        attributes={"service": service},
    ):
        yield


def capture_task_context(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Copy payload and attach only trace and correlation propagation fields."""
    copied = dict(payload)
    copied[_TASK_CONTEXT_KEY] = {
        "correlation_id": correlation_id(),
        **trace_headers(),
    }
    return copied


def pop_task_context(payload: MutableMapping[str, Any]) -> tuple[dict[str, str], uuid.UUID]:
    """Remove reserved task fields and return safe context for worker execution."""
    raw = payload.pop(_TASK_CONTEXT_KEY, None)
    raw_context = raw if isinstance(raw, dict) else {}
    carrier = {
        key: value
        for key, value in raw_context.items()
        if key in {"traceparent", "tracestate"} and isinstance(value, str)
    }
    try:
        corr = uuid.UUID(str(raw_context.get("correlation_id", "")))
    except (TypeError, ValueError, AttributeError):
        corr = uuid.uuid4()
    return carrier, corr


def event_context() -> dict[str, str]:
    """Return the bounded context persisted with an outbox event."""
    return {"correlation_id": correlation_id() or str(uuid.uuid4()), **trace_headers()}


@contextmanager
def restored_correlation(value: object) -> Iterator[None]:
    """Activate a persisted correlation ID only when it remains a valid UUID."""
    try:
        corr = uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        corr = uuid.uuid4()
    token = correlation_id_var.set(corr)
    try:
        yield
    finally:
        correlation_id_var.reset(token)


def record_request(*, service: str, status_code: int, duration_s: float, route: str) -> None:
    """Record an in-process low-cardinality request RED summary."""
    metrics = _service_metrics(service)
    with _METRICS_LOCK:
        metrics.requests += 1
        metrics.request_errors += int(status_code >= 500)
        metrics.request_duration_ms += int(duration_s * 1000)
    span = trace.get_current_span()
    span.set_attribute("http.route", route)
    span.set_attribute("http.response.status_code", status_code)
    span.set_attribute("http.response.status_class", f"{status_code // 100}xx")


def record_worker(*, service: str, outcome: str, duration_s: float, task_kind: str) -> None:
    """Record a low-cardinality worker outcome without task payload data."""
    metrics = _service_metrics(service)
    with _METRICS_LOCK:
        metrics.workers += 1
        metrics.worker_errors += int(outcome != "success")
        metrics.worker_duration_ms += int(duration_s * 1000)
    span = trace.get_current_span()
    span.set_attribute("task.kind", task_kind)
    span.set_attribute("task.outcome", outcome)


def metrics_snapshot(service: str) -> dict[str, int]:
    """Return bounded aggregate RED counters for authenticated diagnostics."""
    metrics = _service_metrics(service)
    with _METRICS_LOCK:
        workers = _METRICS.get("worker", _ServiceMetrics())
        return {
            "requests": metrics.requests,
            "request_errors": metrics.request_errors,
            "request_duration_ms": metrics.request_duration_ms,
            "workers": metrics.workers + workers.workers,
            "worker_errors": metrics.worker_errors + workers.worker_errors,
            "worker_duration_ms": metrics.worker_duration_ms + workers.worker_duration_ms,
        }


def configure_telemetry(
    *, service: str, enabled: bool, otlp_endpoint: str | None, timeout_ms: int
) -> bool:
    """Install one process provider and an optional bounded generic OTLP exporter."""
    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        provider = TracerProvider(resource=Resource.create({"service.name": service}))
        trace.set_tracer_provider(provider)
    if not enabled or not otlp_endpoint or otlp_endpoint in _EXPORT_ENDPOINTS:
        return True
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    exporter = _JarvisSpanExporter(
        OTLPSpanExporter(endpoint=otlp_endpoint, timeout=timeout_ms / 1000)
    )
    provider.add_span_processor(BatchSpanProcessor(exporter, export_timeout_millis=timeout_ms))
    _EXPORT_ENDPOINTS.add(otlp_endpoint)
    return True


def flush_telemetry(*, timeout_ms: int = 5_000) -> None:
    """Boundedly flush the process provider without shutting it down per lifecycle."""
    provider = trace.get_tracer_provider()
    if isinstance(provider, TracerProvider):
        try:
            provider.force_flush(timeout_millis=max(timeout_ms, 0))
        except Exception:
            return


__all__ = [
    "capture_task_context",
    "configure_telemetry",
    "correlation_id",
    "event_context",
    "flush_telemetry",
    "metrics_snapshot",
    "pop_task_context",
    "record_request",
    "record_worker",
    "restored_correlation",
    "request_span",
    "restored_span",
    "trace_headers",
    "trace_id",
]
