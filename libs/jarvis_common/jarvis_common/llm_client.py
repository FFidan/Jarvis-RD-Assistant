"""Shared LiteLLM request helpers for paper-ingestion modules and scripts."""

import logging
import os
import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, TypeVar

import httpx
from pydantic import BaseModel

if TYPE_CHECKING:
    import openai

try:
    from langfuse.decorators import observe
except ImportError:

    def observe(**kwargs):  # type: ignore[misc]
        def decorator(fn):  # type: ignore[misc]
            return fn

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

    litellm runs as a transparent loopback proxy (127.0.0.1:4000) fronting
    Ollama only; no auth is required.  Cloud LLM keys flow direct
    app→provider via encrypted user_config rows, bypassing litellm entirely.
    Reintroduce api_key here only if port 4000 is ever exposed beyond loopback.
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


def get_litellm_config(
    *,
    base_url_default: str = DEFAULT_LITELLM_BASE_URL,
) -> LiteLLMConfig:
    """Resolve LiteLLM base URL from the environment.

    No auth is wired: litellm runs as a transparent loopback proxy and needs
    no master_key.  See LiteLLMConfig docstring for the rationale.
    """
    return LiteLLMConfig(
        base_url=os.environ.get("LITELLM_BASE_URL", base_url_default),
    )


def build_litellm_headers(config: LiteLLMConfig) -> dict[str, str]:  # noqa: ARG001
    """Return auth headers for a LiteLLM request.

    When LITELLM_MASTER_KEY is set, returns ``{"Authorization": "Bearer <key>"}``.
    When unset (e.g. dev without a key configured), returns ``{}`` so that
    loopback-only enforcement still protects the endpoint.
    """
    master_key = os.environ.get("LITELLM_MASTER_KEY")
    if master_key:
        return {"Authorization": f"Bearer {master_key}"}
    return {}


def strip_think_blocks(raw: str) -> str:
    """Strip thinking-model markup before downstream JSON parsing."""
    return re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()


async def request_chat_completion_content(
    http_client: httpx.AsyncClient,
    *,
    prompt: str | None = None,
    messages: list[dict[str, str]] | None = None,
    options: ChatCompletionOptions,
    config: LiteLLMConfig | None = None,
) -> str:
    """Request a LiteLLM chat completion and return stripped message content."""
    litellm = config or get_litellm_config()
    if messages is None:
        if prompt is None:
            raise ValueError("Either prompt or messages must be provided")
        messages = []
        if options.system:
            messages.append({"role": "system", "content": options.system})
        messages.append({"role": "user", "content": prompt})
    payload: dict[str, Any] = {
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
        raw = resp.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("Malformed LLM response") from exc
    if not isinstance(raw, str):
        raise ValueError("Malformed LLM response")
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
    import instructor  # noqa: PLC0415
    from instructor import Mode  # noqa: PLC0415

    _options = options or ChatCompletionOptions()
    _config = config or get_litellm_config()
    _messages: list[dict[str, str]] = list(messages) if messages else []
    if prompt:
        if _options.system and not _messages:
            _messages = [{"role": "system", "content": _options.system}]
        _messages = _messages + [{"role": "user", "content": prompt}]
    elif not _messages:
        raise ValueError("Either prompt or messages must be provided")

    client = instructor.from_openai(openai_client, mode=Mode.JSON)
    return await client.chat.completions.create(
        model=_options.model or _config.base_url,
        response_model=response_model,
        messages=_messages,  # type: ignore[arg-type]
        max_tokens=_options.max_tokens,
        temperature=_options.temperature,
        max_retries=max_retries,
    )


def _langfuse_lifespan_hook() -> None:
    """Initialize Langfuse SDK. Call once at app startup.

    No-op (logs info) if ``LANGFUSE_HOST`` is not set — local dev without the
    ``--profile observability`` stack should not crash on missing env vars.
    """
    host = os.environ.get("LANGFUSE_HOST")
    if not host:
        logger.info("LANGFUSE_HOST unset; Langfuse traces will no-op")
        return
    try:
        from langfuse import Langfuse  # noqa: PLC0415

        Langfuse(
            host=host,
            public_key=os.environ["LANGFUSE_PUBLIC_KEY"],
            secret_key=os.environ["LANGFUSE_SECRET_KEY"],
        )
        logger.info("Langfuse configured, tracing to %s", host)
    except Exception as exc:
        logger.warning("Langfuse init failed (non-fatal): %s", exc)


@observe(as_type="generation")
async def embed_texts(
    http_client: httpx.AsyncClient,
    texts: list[str],
    *,
    model: str = "embed",
    timeout: float = 60.0,
    config: LiteLLMConfig | None = None,
) -> list[list[float]]:
    """Request embeddings from LiteLLM and return them in index order."""
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
