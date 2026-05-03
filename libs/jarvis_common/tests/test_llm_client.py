"""Direct tests for the shared LiteLLM chat helper."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from jarvis_common import llm_client


def test_get_litellm_config_reads_base_url_from_env(monkeypatch):
    """get_litellm_config should resolve LITELLM_BASE_URL from environment."""
    monkeypatch.setenv("LITELLM_BASE_URL", "http://litellm.test:4000")

    config = llm_client.get_litellm_config()

    assert config == llm_client.LiteLLMConfig(base_url="http://litellm.test:4000")


def test_get_litellm_config_uses_default_url(monkeypatch):
    """get_litellm_config should fall back to DEFAULT_LITELLM_BASE_URL."""
    monkeypatch.delenv("LITELLM_BASE_URL", raising=False)

    config = llm_client.get_litellm_config()

    assert config.base_url == llm_client.DEFAULT_LITELLM_BASE_URL


def test_build_litellm_headers_always_returns_empty_dict():
    """build_litellm_headers must return {} — transparent proxy, no auth."""
    config = llm_client.LiteLLMConfig(base_url="http://litellm.test:4000")

    assert llm_client.build_litellm_headers(config) == {}


def test_strip_think_blocks_removes_multiple_sections():
    """strip_think_blocks should remove all think blocks before JSON parsing."""
    raw = '<think>draft</think>\n{"step":1}\n<think>hidden</think>\n{"answer":"ok"}'

    cleaned = llm_client.strip_think_blocks(raw)

    assert "<think>" not in cleaned
    assert cleaned == '{"step":1}\n\n{"answer":"ok"}'


def test_chat_completion_options_with_response_format_preserves_other_fields():
    """with_response_format should only swap the response format field."""
    options = llm_client.ChatCompletionOptions(
        model="smart",
        max_tokens=77,
        temperature=0.3,
        timeout=45.0,
    )

    updated = options.with_response_format({"type": "json_object"})

    assert updated.response_format == {"type": "json_object"}
    assert updated.model == "smart"
    assert updated.max_tokens == 77
    assert updated.temperature == 0.3
    assert updated.timeout == 45.0


@pytest.mark.asyncio
async def test_request_chat_completion_content_requires_prompt_or_messages():
    """The low-level helper should reject empty invocations before any HTTP call."""
    with pytest.raises(ValueError, match="Either prompt or messages must be provided"):
        await llm_client.request_chat_completion_content(
            AsyncMock(),
            options=llm_client.ChatCompletionOptions(),
        )


@pytest.mark.asyncio
async def test_request_chat_completion_content_forwards_response_format():
    """The lower-level request helper should pass through response_format unchanged."""
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {
        "choices": [{"message": {"content": '<think>hidden</think>\n{"answer":"ok"}'}}]
    }
    http_client = AsyncMock()
    http_client.post.return_value = response

    result = await llm_client.request_chat_completion_content(
        http_client,
        prompt="Summarize this.",
        options=llm_client.ChatCompletionOptions(
            model="smart",
            max_tokens=77,
            temperature=0.3,
            timeout=45.0,
            response_format={"type": "json_object"},
        ),
        config=llm_client.LiteLLMConfig(base_url="http://litellm.test:4000"),
    )

    assert result == '{"answer":"ok"}'
    http_client.post.assert_awaited_once_with(
        "http://litellm.test:4000/v1/chat/completions",
        json={
            "model": "smart",
            "messages": [{"role": "user", "content": "Summarize this."}],
            "max_tokens": 77,
            "temperature": 0.3,
            "response_format": {"type": "json_object"},
        },
        headers={},
        timeout=45.0,
    )


@pytest.mark.asyncio
async def test_embed_texts_sorts_embeddings_by_index():
    """embed_texts should restore original input order using LiteLLM indexes."""
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {
        "data": [
            {"index": 1, "embedding": [2.0]},
            {"index": 0, "embedding": [1.0]},
        ]
    }
    http_client = AsyncMock()
    http_client.post.return_value = response

    result = await llm_client.embed_texts(
        http_client,
        ["first", "second"],
        config=llm_client.LiteLLMConfig(base_url="http://litellm.test:4000"),
    )

    assert result == [[1.0], [2.0]]
