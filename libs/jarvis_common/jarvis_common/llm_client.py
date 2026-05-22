"""Shared LiteLLM request helpers for paper-ingestion modules and scripts."""

import functools
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, TypedDict, TypeVar, cast, overload

import httpx
from pydantic import BaseModel

from jarvis_common.config import get_jarvis_common_settings
from jarvis_common.litellm_observer import record_serve
from jarvis_common.settings import get_secrets_settings

if TYPE_CHECKING:
    import openai

_ObservedFn = TypeVar("_ObservedFn", bound=Callable[..., Any])

try:
    from langfuse.decorators import observe  # type: ignore[import-not-found]
except ImportError:
    try:
        # langfuse 3.x / 4.x — observe re-exported at the package root.
        from langfuse import observe  # type: ignore[no-redef]
    except ImportError:

        @overload
        def observe(fn: _ObservedFn, /) -> _ObservedFn: ...

        @overload
        def observe(*args: Any, **kwargs: Any) -> Callable[[_ObservedFn], _ObservedFn]: ...

        def observe(*args: Any, **kwargs: Any) -> Any:
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


T = TypeVar("T", bound=BaseModel)

logger = logging.getLogger(__name__)

DEFAULT_LITELLM_BASE_URL = "http://litellm:4000"
LLM_TIMEOUT_SHORT = 30.0
LLM_TIMEOUT_DEFAULT = 120.0
LLM_TIMEOUT_LONG = 300.0


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


def strip_think_blocks(raw: str) -> str:
    """Strip thinking-model markup before downstream JSON parsing."""
    return re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()


def strip_think_streaming(chunk: str, in_think: bool, carry: str = "") -> tuple[str, bool, str]:
    """Stateful streaming filter that drops <think>...</think> blocks across chunks.

    Tokens may split arbitrarily across SSE deltas (e.g. ``<th`` + ``ink>``).
    Caller threads (in_think, carry) across calls; carry holds a partial
    open/close tag that straddles the chunk boundary.

    Returns (visible_text, new_in_think_state, new_carry).
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
    http_client:
        Shared ``httpx.AsyncClient`` from the service lifespan.
    prompt:
        Convenience single user-role message.  Supply either *prompt* or
        *messages*, not both independently (if both are provided, *messages*
        is used as-is and *prompt* is ignored).
    messages:
        Full message list.  When ``None`` a list is built from *options.system*
        + *prompt*.
    options:
        Model, token, temperature, and timeout settings.
    config:
        LiteLLM connection config; defaults to env-resolved config.

    Returns
    -------
    str
        Model response text with think-block markup stripped.

    Raises
    ------
    RuntimeError
        On HTTP error, timeout, or connection failure.
    ValueError
        If the response body does not contain ``choices[0].message.content``.
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
    return strip_think_blocks(raw)


@observe(as_type="generation")
async def call_llm_structured(
    openai_client: "openai.AsyncOpenAI",
    *,
    response_model: type[T],
    prompt: str | None = None,
    messages: list[dict[str, str]] | None = None,
    options: ChatCompletionOptions | None = None,
    config: LiteLLMConfig | None = None,
    max_retries: int = 2,
) -> T:
    """Structured LLM call via Instructor. Returns a validated Pydantic instance.

    Parameters
    ----------
    openai_client:
        An ``openai.AsyncOpenAI`` client patched with ``instructor.from_openai``.
        Build once in the service lifespan (see ``_langfuse_lifespan_hook``).
    response_model:
        Pydantic model class that defines the expected response shape.
    prompt:
        Convenience shorthand for a single user-role message.  Mutually
        exclusive with ``messages``.
    messages:
        Full message list (system + user).  If both ``prompt`` and ``messages``
        are provided, ``prompt`` is appended as a final user message.
    options:
        Model / token / temperature options.  Defaults to ChatCompletionOptions().
    config:
        LiteLLM config; defaults to env-resolved config.
    max_retries:
        Instructor retry budget (default 2).
    """
    _options = options or ChatCompletionOptions()
    _config = config or get_litellm_config()
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

    # openai_client is already instructor-patched (wrapped in the service lifespan).
    # Do NOT call instructor.from_openai() again — double-wrapping returns None on
    # some instructor versions, causing 'NoneType has no attribute chat'.
    result = await openai_client.chat.completions.create(
        model=_options.model,
        response_model=response_model,
        messages=_messages,  # type: ignore[arg-type]
        max_tokens=_options.max_tokens,
        temperature=_options.temperature,
        timeout=_options.timeout,
        max_retries=max_retries,
    )
    if _options.model == "smart":
        # Instructor attaches the raw ChatCompletion as _raw_response on the result.
        raw_resp = getattr(result, "_raw_response", None)
        served = getattr(raw_resp, "model", None) or _options.model
        record_serve(_options.model, served)
    return result


def _langfuse_lifespan_hook() -> None:
    """Init Langfuse once at startup. No-op (logs) unless OBSERVABILITY_ENABLED
    and host+keys are all present.

    Design constraints (do NOT relax):
    * Runs as the FIRST init task, before DB migrations — must touch NO database.
    * Must NEVER raise: every disabled/misconfigured combination returns cleanly
      so it cannot break startup.  The broad ``except Exception`` is load-bearing.
    * The enable-gate (``OBSERVABILITY_ENABLED``) and host (``LANGFUSE_HOST``)
      are read from :class:`jarvis_common.config.JarvisCommonSettings` (plain
      env vars, set by compose as-is).
    * Keys (``LANGFUSE_PUBLIC_KEY`` / ``LANGFUSE_SECRET_KEY``) are read from
      :class:`jarvis_common.settings.SecretsSettings` — the ``_FILE``-aware
      model — so compose's ``LANGFUSE_PUBLIC_KEY_FILE=/run/secrets/…`` indirection
      is honoured without any custom resolver.  Never read from ``os.environ``
      directly, which would ``KeyError`` when absent.
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
        logger.warning("OBSERVABILITY_ENABLED set but LANGFUSE_HOST/keys missing; traces no-op")
        return
    try:
        from langfuse import Langfuse  # noqa: PLC0415

        Langfuse(host=host, public_key=pk, secret_key=sk)
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
    http_client:
        Shared ``httpx.AsyncClient`` from the service lifespan.
    texts:
        List of strings to embed.  An empty list returns ``[]`` immediately.
    model:
        LiteLLM model alias (default ``"embed"``).
    timeout:
        HTTP timeout in seconds (default 60.0).
    config:
        LiteLLM connection config; defaults to env-resolved config.

    Returns
    -------
    list[list[float]]
        One embedding vector per input string, in the same order as *texts*.

    Raises
    ------
    RuntimeError
        On HTTP error, timeout, connection failure, or malformed response body.
    """
    if not texts:
        return []

    litellm = config or get_litellm_config()
    try:
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
        return [item["embedding"] for _, item in data]
    except (KeyError, TypeError, IndexError) as exc:
        raise RuntimeError(f"Unexpected embedding response format: {exc}") from exc
