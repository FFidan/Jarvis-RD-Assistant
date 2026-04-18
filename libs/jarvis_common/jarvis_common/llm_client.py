"""Shared LiteLLM request helpers for paper-ingestion modules and scripts."""

import json
import logging
import os
import re
from dataclasses import dataclass, replace
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_LITELLM_BASE_URL = "http://litellm:4000"
DEFAULT_LITELLM_PRIMARY_ENV_NAME = "LITELLM_API_KEY"
LITELLM_FALLBACK_ENV_NAMES = ("LITELLM_MASTER_KEY",)
LLM_TIMEOUT_SHORT = 30.0
LLM_TIMEOUT_DEFAULT = 120.0
LLM_TIMEOUT_LONG = 300.0


@dataclass(frozen=True)
class LiteLLMConfig:
    """Resolved LiteLLM connection settings."""

    base_url: str
    api_key: str


@dataclass(frozen=True)
class ChatCompletionOptions:
    """Model and timeout settings for a non-streaming LiteLLM chat request."""

    model: str = "smart"
    max_tokens: int = 2000
    temperature: float = 0.1
    timeout: float = LLM_TIMEOUT_DEFAULT
    response_format: dict[str, str] | None = None

    def with_response_format(
        self, response_format: dict[str, str] | None
    ) -> "ChatCompletionOptions":
        """Return a copy of the options with a different response format."""
        return replace(self, response_format=response_format)


def get_litellm_config(
    *,
    base_url_default: str = DEFAULT_LITELLM_BASE_URL,
    primary_env_name: str = DEFAULT_LITELLM_PRIMARY_ENV_NAME,
    fallback_env_names: tuple[str, ...] = (),
) -> LiteLLMConfig:
    """Resolve LiteLLM base URL and auth settings from the environment."""
    api_key = os.environ.get(primary_env_name, "")
    if not api_key:
        for env_name in fallback_env_names:
            api_key = os.environ.get(env_name, "")
            if api_key:
                break

    return LiteLLMConfig(
        base_url=os.environ.get("LITELLM_BASE_URL", base_url_default),
        api_key=api_key,
    )


def build_litellm_headers(config: LiteLLMConfig) -> dict[str, str]:
    """Build auth headers for a LiteLLM request."""
    return {"Authorization": f"Bearer {config.api_key}"} if config.api_key else {}


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
        messages = [{"role": "user", "content": prompt}]
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


async def call_llm_json_value(
    http_client: httpx.AsyncClient,
    prompt: str,
    *,
    options: ChatCompletionOptions | None = None,
    response_format: dict[str, str] | None = None,
    config: LiteLLMConfig | None = None,
) -> Any:
    """Call LiteLLM and parse the response content as JSON."""
    resolved_options = options or ChatCompletionOptions()
    if response_format is not None:
        resolved_options = resolved_options.with_response_format(response_format)
    raw = await request_chat_completion_content(
        http_client,
        prompt=prompt,
        options=resolved_options,
        config=config,
    )
    stripped = raw.strip()
    if not stripped or stripped[0] not in ("{", "["):
        raise ValueError(
            f"LLM returned non-JSON content (expected '{{' or '[', got {stripped[:50]!r})"
        )
    try:
        return json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM returned invalid JSON: {raw[:200]}") from exc


async def call_llm(
    http_client: httpx.AsyncClient,
    prompt: str,
    *,
    options: ChatCompletionOptions | None = None,
    config: LiteLLMConfig | None = None,
) -> dict[str, Any]:
    """Call LiteLLM and parse JSON response, stripping thinking-model artifacts."""
    resolved_options = options or ChatCompletionOptions()
    if resolved_options.response_format is None:
        resolved_options = resolved_options.with_response_format({"type": "json_object"})
    parsed = await call_llm_json_value(
        http_client,
        prompt,
        options=resolved_options,
        config=config,
    )
    if not isinstance(parsed, dict):
        raise ValueError("LiteLLM returned non-object JSON for an object-only request")
    return parsed


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
