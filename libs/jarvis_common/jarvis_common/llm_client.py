"""Shared LiteLLM request helpers for paper-ingestion modules and scripts."""

import functools
import json
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, TypedDict, TypeVar, cast, overload

import httpx
from opentelemetry.sdk.trace.export import SpanExporter
from pydantic import BaseModel

from jarvis_common.config import get_jarvis_common_settings
from jarvis_common.litellm_observer import record_serve
from jarvis_common.maintenance import (
    OutboundEgressBlockedError,
    ensure_outbound_egress_allowed,
)
from jarvis_common.settings import get_secrets_settings

if TYPE_CHECKING:
    import openai

_ObservedFn = TypeVar("_ObservedFn", bound=Callable[..., Any])

try:
    from langfuse.decorators import observe as _langfuse_observe  # type: ignore[import-not-found]
except ImportError:
    try:
        # langfuse 3.x / 4.x — observe re-exported at the package root.
        from langfuse import observe as _langfuse_observe
    except ImportError:

        @overload
        def _langfuse_observe(fn: _ObservedFn, /) -> _ObservedFn: ...

        @overload
        def _langfuse_observe(
            *args: Any, **kwargs: Any
        ) -> Callable[[_ObservedFn], _ObservedFn]: ...

        def _langfuse_observe(*args: Any, **kwargs: Any) -> Any:
            """No-op fallback when langfuse is not installed.

            Uses ``functools.wraps`` so callers (and tests) can still rely on
            ``__wrapped__`` to assert that ``@observe()`` is present on
            trace-boundary functions per docs/contracts/04-observability.md.
            """

            def decorator(fn: _ObservedFn) -> _ObservedFn:
                @functools.wraps(fn)
                async def _async_wrapper(*a: Any, **kw: Any) -> Any:
                    return await fn(*a, **kw)

                @functools.wraps(fn)
                def _sync_wrapper(*a: Any, **kw: Any) -> Any:
                    return fn(*a, **kw)

                import asyncio  # noqa: PLC0415

                wrapper = _async_wrapper if asyncio.iscoroutinefunction(fn) else _sync_wrapper
                return cast(_ObservedFn, wrapper)

            # Support both @observe and @observe(...) call styles.
            if args and callable(args[0]) and not kwargs:
                return decorator(args[0])
            return decorator


@overload
def observe(fn: _ObservedFn, /) -> _ObservedFn: ...


@overload
def observe(*args: Any, **kwargs: Any) -> Callable[[_ObservedFn], _ObservedFn]: ...


def observe(*args: Any, **kwargs: Any) -> Any:
    """Create a Langfuse span without implicitly serializing call objects.

    The SDK decorator captures every positional argument and return value by
    default. JARVIS trace boundaries routinely receive database pools, HTTP
    clients, and full retrieval objects, so automatic capture is both unsafe
    and unbounded. Generation call sites add explicitly bounded content with
    :func:`record_generation_observation`.

    Parameters
    ----------
    *args : Any
        Positional arguments accepted by the installed Langfuse decorator.
    **kwargs : Any
        Keyword arguments accepted by the installed Langfuse decorator.

    Returns
    -------
    Any
        The decorated callable or decorator returned by Langfuse.
    """
    safe_kwargs = dict(kwargs)
    safe_kwargs.setdefault("capture_input", False)
    safe_kwargs.setdefault("capture_output", False)
    return _langfuse_observe(*args, **safe_kwargs)


T = TypeVar("T", bound=BaseModel)

logger = logging.getLogger(__name__)

DEFAULT_LITELLM_BASE_URL = "http://litellm:4000"
LLM_TIMEOUT_SHORT = 30.0
LLM_TIMEOUT_DEFAULT = 120.0
LLM_TIMEOUT_LONG = 300.0
_LANGFUSE_CONTENT_LIMIT = 20_000
_OBSERVATION_UNSET = object()

_WORK_NOTE_MARKER_RE = re.compile(
    r"^\s*(?:"
    r"let\s+me\b|"
    r"let['’]s\s+(?:look|analy[sz]e|think|break|work)\b|"
    r"i\s+(?:need|should|will)\s+(?:to\s+)?"
    r"(?:look|analy[sz]e|think|check|determine|compare|answer)\b|"
    r"i['’]ll\s+"
    r"(?:look|analy[sz]e|think|check|determine|compare|answer)\b|"
    r"i\s+am\s+going\s+to\s+"
    r"(?:look|analy[sz]e|think|check|determine|compare|answer)\b|"
    r"first,?\s+(?:i\s+(?:need|should|will)|i['’]ll)\b"
    r")",
    re.IGNORECASE,
)


_WORK_NOTE_PREFIX_MARKERS = (
    "let me",
    "let's look",
    "let's analyse",
    "let's analyze",
    "let's think",
    "let's break",
    "let's work",
    "i need look",
    "i need to look",
    "i need analyse",
    "i need to analyse",
    "i need analyze",
    "i need to analyze",
    "i need think",
    "i need to think",
    "i need check",
    "i need to check",
    "i need determine",
    "i need to determine",
    "i need compare",
    "i need to compare",
    "i need answer",
    "i need to answer",
    "i should look",
    "i should to look",
    "i should analyse",
    "i should to analyse",
    "i should analyze",
    "i should to analyze",
    "i should think",
    "i should to think",
    "i should check",
    "i should to check",
    "i should determine",
    "i should to determine",
    "i should compare",
    "i should to compare",
    "i should answer",
    "i should to answer",
    "i will look",
    "i will to look",
    "i will analyse",
    "i will to analyse",
    "i will analyze",
    "i will to analyze",
    "i will think",
    "i will to think",
    "i will check",
    "i will to check",
    "i will determine",
    "i will to determine",
    "i will compare",
    "i will to compare",
    "i will answer",
    "i will to answer",
    "i'll look",
    "i am going to look",
    "i'll analyse",
    "i am going to analyse",
    "i'll analyze",
    "i am going to analyze",
    "i'll think",
    "i am going to think",
    "i'll check",
    "i am going to check",
    "i'll determine",
    "i am going to determine",
    "i'll compare",
    "i am going to compare",
    "i'll answer",
    "i am going to answer",
    "first i need",
    "first, i need",
    "first i should",
    "first, i should",
    "first i will",
    "first, i will",
    "first i'll",
    "first, i'll",
)


class EmptyVisibleLLMContentError(RuntimeError):
    """Raised when a scalar chat response has no user-visible content."""


@dataclass(frozen=True)
class VisibleWorkNoteDetection:
    """Classification for visible model work notes in user-facing answers.

    Attributes
    ----------
    has_work_notes : bool
        Whether the visible answer starts with reasoning/process prose that
        should not be shown as the final RAG answer.
    marker : str or None
        The matched leading marker, when available. The marker is safe for
        assertions and metrics but never contains the discarded answer text.

    """

    has_work_notes: bool
    marker: str | None = None


@dataclass(frozen=True)
class LiteLLMConfig:
    """Resolved LiteLLM connection settings.

    LiteLLM remains loopback-bound in local deployments.  When
    ``LITELLM_MASTER_KEY`` is configured, callers send it as a bearer token via
    :func:`build_litellm_headers`; when unset, callers use the loopback-only
    fallback with no Authorization header.
    """

    base_url: str


@dataclass(frozen=True)
class ChatCompletionOptions:
    """Model and timeout settings for a non-streaming LiteLLM chat request."""

    model: str = "smart"
    max_tokens: int = 2000
    temperature: float = 0.1
    timeout: float = LLM_TIMEOUT_DEFAULT
    response_format: dict[str, str] | None = None
    system: str | None = None  # Optional system prompt sent as a system role message

    def with_response_format(
        self, response_format: dict[str, str] | None
    ) -> "ChatCompletionOptions":
        """Return a copy of the options with a different response format."""
        return replace(self, response_format=response_format)


class _ChatCompletionPayload(TypedDict, total=False):
    """Wire payload for a LiteLLM /v1/chat/completions request.

    ``model``, ``messages``, ``max_tokens``, and ``temperature`` are always
    present (``total=False`` used for the optional ``response_format`` key).
    """

    model: str
    messages: list[dict[str, str]]
    max_tokens: int
    temperature: float
    response_format: dict[str, str]


def get_litellm_config(
    *,
    base_url_default: str = DEFAULT_LITELLM_BASE_URL,
) -> LiteLLMConfig:
    """Resolve LiteLLM base URL from the environment.

    Authentication headers are resolved separately by
    :func:`build_litellm_headers` so tests and call sites can share the same
    base-url config object.
    """
    return LiteLLMConfig(
        base_url=os.environ.get("LITELLM_BASE_URL", base_url_default),
    )


def build_litellm_headers(config: LiteLLMConfig) -> dict[str, str]:  # noqa: ARG001
    """Return auth headers for a LiteLLM request.

    Resolves ``LITELLM_MASTER_KEY`` via :class:`jarvis_common.settings.SecretsSettings`
    so the Docker Secret ``LITELLM_MASTER_KEY_FILE`` mount is honoured before
    the legacy plain env var.  When the key resolves to a non-empty value,
    returns ``{"Authorization": "Bearer <key>"}``.  When unset (e.g. dev
    without a key configured), returns ``{}`` so that loopback-only enforcement
    still protects the endpoint.
    """
    from jarvis_common.settings import get_secrets_settings  # noqa: PLC0415 — lazy to avoid cycles

    secret = get_secrets_settings().litellm_master_key
    master_key = secret.get_secret_value() if secret else ""
    if master_key:
        return {"Authorization": f"Bearer {master_key}"}
    return {}


def _normalize_visible_work_note_prefix(answer: str) -> str:
    """Normalize a candidate work-note prefix for partial-stream checks."""
    normalized = answer.lower().replace("’", "'")
    return " ".join(normalized.strip().split())


def could_be_visible_work_note_prefix(answer: str) -> bool:
    """Return whether partial text can still match a blocked status marker.

    Parameters
    ----------
    answer : str
        Partial user-visible model output.

    Returns
    -------
    bool
        Whether more streamed text could complete a blocked leading marker.

    """
    normalized = _normalize_visible_work_note_prefix(answer)
    if not normalized:
        return True
    return any(marker.startswith(normalized) for marker in _WORK_NOTE_PREFIX_MARKERS)


def detect_visible_work_notes(answer: str) -> VisibleWorkNoteDetection:
    """Detect leading status text that should not appear in a final answer.

    The detector is deliberately conservative: it catches common leading
    status markers while allowing ordinary final-answer language such as
    "The problem is..." or domain uses of "analysis".

    Parameters
    ----------
    answer : str
        Complete candidate answer.

    Returns
    -------
    VisibleWorkNoteDetection
        Whether a blocked marker was found and its normalized text when present.

    """
    match = _WORK_NOTE_MARKER_RE.search(answer)
    if match is None:
        return VisibleWorkNoteDetection(has_work_notes=False)
    return VisibleWorkNoteDetection(
        has_work_notes=True,
        marker=" ".join(match.group(0).strip().split()).lower(),
    )


def strip_think_blocks(raw: str) -> str:
    """Remove nested ``<think>`` regions before parsing model output.

    Parameters
    ----------
    raw : str
        Model output that may contain nested or unterminated thinking regions.

    Returns
    -------
    str
        Visible text outside the thinking regions, stripped at both ends.

    """
    open_tag, close_tag = "<think>", "</think>"
    out: list[str] = []
    depth = 0
    i = 0
    while i < len(raw):
        if depth == 0:
            j = raw.find(open_tag, i)
            if j == -1:
                out.append(raw[i:])
                break
            out.append(raw[i:j])
            i = j + len(open_tag)
            depth = 1
        else:
            open_j = raw.find(open_tag, i)
            close_j = raw.find(close_tag, i)
            if close_j == -1:
                # Unterminated — discard everything remaining (including the open tag we entered).
                break
            if open_j != -1 and open_j < close_j:
                # Nested open tag encountered first; increase depth.
                i = open_j + len(open_tag)
                depth += 1
            else:
                # Close tag encountered; decrease depth.
                i = close_j + len(close_tag)
                depth -= 1
    return "".join(out).strip()


def strip_think_streaming(chunk: str, in_think: bool, carry: str = "") -> tuple[str, bool, str]:
    """Remove ``<think>`` regions that may span streamed chunks.

    Tokens may split arbitrarily across SSE deltas (e.g. ``<th`` + ``ink>``).
    The caller passes ``in_think`` and ``carry`` to the next call; ``carry`` holds a partial
    open/close tag that straddles the chunk boundary.

    Parameters
    ----------
    chunk : str
        Next streamed text fragment.
    in_think : bool
        Whether the preceding fragment ended inside a thinking region.
    carry : str
        Partial opening or closing tag retained from the preceding fragment.

    Returns
    -------
    tuple[str, bool, str]
        Visible text, the updated thinking-region state, and the next partial-tag
        carry value.

    """
    buf = carry + chunk
    out: list[str] = []
    i = 0
    open_tag, close_tag = "<think>", "</think>"
    while i < len(buf):
        if in_think:
            j = buf.find(close_tag, i)
            if j == -1:
                # Possible partial close-tag at end of buffer — hold up to len(close_tag)-1 chars.
                # Anything before tail_keep is definitely inside the think block; drop it.
                # Anything in [tail_keep, len(buf)) might be the start of close_tag; carry it.
                tail_keep = max(0, len(buf) - (len(close_tag) - 1))
                return "".join(out), True, buf[tail_keep:]
            i = j + len(close_tag)
            in_think = False
        else:
            j = buf.find(open_tag, i)
            if j == -1:
                # No open-tag in remainder. Hold a tail in case it's a partial open_tag.
                tail_keep = max(i, len(buf) - (len(open_tag) - 1))
                out.append(buf[i:tail_keep])
                return "".join(out), False, buf[tail_keep:]
            out.append(buf[i:j])
            i = j + len(open_tag)
            in_think = True
    return "".join(out), in_think, ""


async def request_chat_completion_content(
    http_client: httpx.AsyncClient,
    *,
    prompt: str | None = None,
    messages: list[dict[str, str]] | None = None,
    options: ChatCompletionOptions,
    config: LiteLLMConfig | None = None,
) -> str:
    """Request a non-streaming LiteLLM chat completion and return the text content.

    Sends to ``POST {config.base_url}/v1/chat/completions``, parses
    ``choices[0].message.content``, and passes the result through
    :func:`strip_think_blocks` to remove ``<think>…</think>`` markup emitted
    by reasoning models.

    Parameters
    ----------
    http_client : httpx.AsyncClient
        Shared ``httpx.AsyncClient`` from the service lifespan.
    prompt : str or None
        Convenience single user-role message.  Supply either *prompt* or
        *messages*, not both independently (if both are provided, *messages*
        is used as-is and *prompt* is ignored).
    messages : list[dict[str, str]] or None
        Full message list.  When ``None`` a list is built from *options.system*
        + *prompt*.
    options : ChatCompletionOptions
        Model, token, temperature, and timeout settings.
    config : LiteLLMConfig or None
        LiteLLM connection config; defaults to env-resolved config.

    Returns
    -------
    str
        Model response text with think-block markup stripped.

    Raises
    ------
    OutboundEgressBlockedError
        If restored credentials await review when the request is about to send.
    RuntimeError
        On HTTP error, timeout, or connection failure.
    EmptyVisibleLLMContentError
        If the response only contains whitespace or stripped think-block text.
    ValueError
        If neither ``prompt`` nor ``messages`` is provided, or if the response
        body does not contain ``choices[0].message.content``.

    """
    litellm = config or get_litellm_config()
    if messages is None:
        if prompt is None:
            raise ValueError("Either prompt or messages must be provided")
        messages = []
        if options.system:
            messages.append({"role": "system", "content": options.system})
        messages.append({"role": "user", "content": prompt})
    payload: _ChatCompletionPayload = {
        "model": options.model,
        "messages": messages,
        "max_tokens": options.max_tokens,
        "temperature": options.temperature,
    }
    if options.response_format is not None:
        payload["response_format"] = options.response_format

    try:
        ensure_outbound_egress_allowed("LiteLLM chat completion")
        resp = await http_client.post(
            f"{litellm.base_url}/v1/chat/completions",
            json=payload,
            headers=build_litellm_headers(litellm),
            timeout=options.timeout,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(
            f"LiteLLM chat error {exc.response.status_code}: {exc.response.text[:200]}"
        ) from exc
    except httpx.TimeoutException as exc:
        raise RuntimeError("LiteLLM chat request timed out") from exc
    except httpx.RequestError as exc:
        raise RuntimeError(f"LiteLLM chat request failed: {exc}") from exc
    try:
        body = resp.json()
        raw = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("Malformed LLM response") from exc
    if not isinstance(raw, str):
        raise ValueError("Malformed LLM response")
    if options.model == "smart":
        served = (
            resp.headers.get("x-litellm-model-id")
            or (body.get("model") if isinstance(body, dict) else None)
            or options.model
        )
        record_serve(options.model, served)
    visible = strip_think_blocks(raw)
    if not visible:
        raise EmptyVisibleLLMContentError(
            "LiteLLM chat response contained no visible content after think-block stripping"
        )
    return visible


def _bounded_observation_value(value: object) -> str:
    """Serialize one observation value without retaining an unbounded graph."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    try:
        serialized = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        serialized = f"<{type(value).__name__}>"
    if len(serialized) <= _LANGFUSE_CONTENT_LIMIT:
        return serialized
    suffix = "..._truncated"
    return serialized[: _LANGFUSE_CONTENT_LIMIT - len(suffix)] + suffix


def record_generation_observation(
    *,
    input_value: object = _OBSERVATION_UNSET,
    output_value: object = _OBSERVATION_UNSET,
    model: str | None = None,
) -> None:
    """Attach explicitly bounded content to the active Langfuse generation.

    Parameters
    ----------
    input_value : object
        Prompt or embedding input to serialize, capped at 20,000 characters.
        Omit it when only recording a response.
    output_value : object
        Response to serialize under the same cap. Omit it when only recording
        a prompt.
    model : str or None
        LiteLLM alias or resolved model name for the generation.

    Notes
    -----
    The helper is a no-op when the opt-in observability profile or Langfuse
    credentials are absent. It never raises into the product request path.
    """
    settings = get_jarvis_common_settings()
    if not settings.observability_enabled or not settings.langfuse_host:
        return
    secrets = get_secrets_settings()
    if not secrets.langfuse_public_key or not secrets.langfuse_secret_key:
        return
    has_input = input_value is not _OBSERVATION_UNSET
    has_output = output_value is not _OBSERVATION_UNSET
    if not has_input and not has_output and model is None:
        return
    try:
        from langfuse import get_client  # noqa: PLC0415

        client = get_client()
        if has_input and has_output:
            client.update_current_generation(
                input=_bounded_observation_value(input_value),
                output=_bounded_observation_value(output_value),
                model=model,
            )
        elif has_input:
            client.update_current_generation(
                input=_bounded_observation_value(input_value), model=model
            )
        elif has_output:
            client.update_current_generation(
                output=_bounded_observation_value(output_value), model=model
            )
        else:
            client.update_current_generation(model=model)
    except Exception as exc:  # noqa: BLE001 -- telemetry must remain optional
        logger.debug("Langfuse generation update skipped: %s", type(exc).__name__)


@observe(as_type="generation")
async def call_llm_structured(
    openai_client: "openai.AsyncOpenAI",
    *,
    response_model: type[T],
    prompt: str | None = None,
    messages: list[dict[str, str]] | None = None,
    options: ChatCompletionOptions | None = None,
    max_retries: int = 2,
) -> T:
    """Structured LLM call via Instructor. Returns a validated Pydantic instance.

    Parameters
    ----------
    openai_client : openai.AsyncOpenAI
        An ``openai.AsyncOpenAI`` client patched with ``instructor.from_openai``.
        Build once in the service lifespan (see ``_langfuse_lifespan_hook``).
    response_model : type[T]
        Pydantic model class that defines the expected response shape.
    prompt : str or None
        Convenience shorthand for a single user-role message.  Mutually
        exclusive with ``messages``.
    messages : list[dict[str, str]] or None
        Full message list (system + user).  If both ``prompt`` and ``messages``
        are provided, ``prompt`` is appended as a final user message.
    options : ChatCompletionOptions or None
        Model / token / temperature options.  Defaults to ChatCompletionOptions().
    max_retries : int
        Instructor retry budget (default 2).

    Returns
    -------
    T
        Validated response model returned by Instructor.

    Raises
    ------
    OutboundEgressBlockedError
        If post-restore credential review currently prohibits LLM egress.
    ValueError
        If neither prompt nor messages is provided, or the model alias is empty.
    RuntimeError
        If no patched client is provided.

    """
    _options = options or ChatCompletionOptions()
    _messages: list[dict[str, str]] = list(messages) if messages else []
    if prompt:
        if _options.system and not any(m.get("role") == "system" for m in _messages):
            _messages = [{"role": "system", "content": _options.system}] + _messages
        _messages = _messages + [{"role": "user", "content": prompt}]
    elif not _messages:
        raise ValueError("Either prompt or messages must be provided")

    if openai_client is None:
        raise RuntimeError(
            "openai_client is required for call_llm_structured (typically "
            "svc.openai_client wired up by the service lifespan)"
        )
    if not _options.model:
        raise ValueError(
            "ChatCompletionOptions.model must be a non-empty model alias "
            "(e.g. 'smart' / 'fast'); got an empty value"
        )

    record_generation_observation(input_value=_messages, model=_options.model)

    # openai_client is already instructor-patched (wrapped in the service lifespan).
    # Do NOT call instructor.from_openai() again — double-wrapping returns None on
    # some instructor versions, causing 'NoneType has no attribute chat'.
    ensure_outbound_egress_allowed("structured LLM completion")
    result = await openai_client.chat.completions.create(
        model=_options.model,
        response_model=response_model,
        messages=_messages,  # type: ignore[arg-type]
        max_tokens=_options.max_tokens,
        temperature=_options.temperature,
        timeout=_options.timeout,
        max_retries=max_retries,
    )
    record_generation_observation(output_value=result)
    if _options.model == "smart":
        # Instructor attaches the raw ChatCompletion as _raw_response on the result.
        raw_resp = getattr(result, "_raw_response", None)
        served = getattr(raw_resp, "model", None) or _options.model
        record_serve(_options.model, served)
    return result


def _build_langfuse_span_exporter(base_url: str, public_key: str, secret_key: str) -> SpanExporter:
    """Build the bounded exporter supported by the pinned Langfuse server."""
    from jarvis_common.langfuse_v2_exporter import LangfuseV2SpanExporter  # noqa: PLC0415

    return LangfuseV2SpanExporter(
        base_url=base_url,
        public_key=public_key,
        secret_key=secret_key,
    )


def _langfuse_lifespan_hook() -> None:
    """Initialize Langfuse once when configuration and egress policy permit it.

    Notes
    -----
    This startup hook runs before database migrations and never accesses the
    database. If observability is disabled, configuration is incomplete,
    quarantine is active, or initialization fails, telemetry remains disabled
    and service startup continues. Host settings come from
    :class:`jarvis_common.config.JarvisCommonSettings`; ``_FILE``-aware keys
    come from :class:`jarvis_common.settings.SecretsSettings`.

    """
    settings = get_jarvis_common_settings()
    if not settings.observability_enabled:
        logger.info("OBSERVABILITY_ENABLED is false; Langfuse traces no-op")
        return
    secrets = get_secrets_settings()
    host = settings.langfuse_host
    pk = secrets.langfuse_public_key.get_secret_value() if secrets.langfuse_public_key else None
    sk = secrets.langfuse_secret_key.get_secret_value() if secrets.langfuse_secret_key else None
    if not (host and pk and sk):
        logger.info(
            "OBSERVABILITY_ENABLED set but LANGFUSE_HOST/keys missing; Langfuse traces no-op"
        )
        # Suppress the per-call "Authentication error: … no public_key" WARNING
        # that langfuse emits on every @observe invocation when unconfigured.
        # Raising the threshold to ERROR silences the flood without touching app logs.
        logging.getLogger("langfuse").setLevel(logging.ERROR)
        return
    try:
        ensure_outbound_egress_allowed("Langfuse telemetry initialization")
    except OutboundEgressBlockedError:
        logger.info("Langfuse disabled while restored credentials await review")
        return
    try:
        from langfuse import (
            Langfuse,  # noqa: PLC0415  # pyright: ignore[reportAttributeAccessIssue]
        )

        Langfuse(
            base_url=host,
            public_key=pk,
            secret_key=sk,
            span_exporter=_build_langfuse_span_exporter(host, pk, sk),
        )
        logger.info("Langfuse configured, tracing to %s", host)
    except Exception as exc:  # noqa: BLE001 — must never break startup
        logger.warning("Langfuse init failed (non-fatal): %s", exc, exc_info=True)


@observe(as_type="generation")
async def embed_texts(
    http_client: httpx.AsyncClient,
    texts: list[str],
    *,
    model: str = "embed",
    timeout: float = 60.0,
    config: LiteLLMConfig | None = None,
) -> list[list[float]]:
    """Request text embeddings from LiteLLM and return them in input order.

    Posts to ``POST {config.base_url}/v1/embeddings``, sorts the returned
    ``data`` array by the per-item ``index`` field, and returns the embedding
    vectors as a list of float lists.

    Parameters
    ----------
    http_client : httpx.AsyncClient
        Shared ``httpx.AsyncClient`` from the service lifespan.
    texts : list[str]
        List of strings to embed.  An empty list returns ``[]`` immediately.
    model : str
        LiteLLM model alias (default ``"embed"``).
    timeout : float
        HTTP timeout in seconds (default 60.0).
    config : LiteLLMConfig or None
        LiteLLM connection config; defaults to env-resolved config.

    Returns
    -------
    list[list[float]]
        One embedding vector per input string, in the same order as *texts*.

    Raises
    ------
    OutboundEgressBlockedError
        If restored credentials await review when a non-empty request is about
        to send. Empty input returns before the egress check.
    RuntimeError
        On HTTP error, timeout, connection failure, or malformed response body.

    """
    if not texts:
        return []

    litellm = config or get_litellm_config()
    record_generation_observation(input_value=texts, model=model)
    try:
        ensure_outbound_egress_allowed("LiteLLM embeddings")
        response = await http_client.post(
            f"{litellm.base_url}/v1/embeddings",
            json={"model": model, "input": texts},
            headers=build_litellm_headers(litellm),
            timeout=timeout,
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(
            f"Embedding request failed with status {exc.response.status_code}"
        ) from exc
    except httpx.TimeoutException as exc:
        raise RuntimeError("Embedding request timed out") from exc
    except httpx.RequestError as exc:
        raise RuntimeError(f"Embedding request failed: {exc}") from exc
    payload = response.json()
    try:
        data = sorted(
            enumerate(payload["data"]),
            key=lambda pair: pair[1].get("index", pair[0]),
        )
        embeddings = [item["embedding"] for _, item in data]
        record_generation_observation(output_value=embeddings)
        return embeddings
    except (KeyError, TypeError, IndexError) as exc:
        raise RuntimeError(f"Unexpected embedding response format: {exc}") from exc
