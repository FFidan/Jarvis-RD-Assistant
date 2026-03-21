"""Direct tests for the shared LiteLLM chat helper."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from jarvis_common import llm_client


@pytest.mark.asyncio
async def test_call_llm_posts_expected_payload(monkeypatch):
    """call_llm should send the standard JSON-object completion payload."""
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {
        "choices": [{"message": {"content": '{"answer": "ok"}'}}]
    }
    http_client = AsyncMock()
    http_client.post.return_value = response
    config = llm_client.LiteLLMConfig(
        base_url="http://litellm.test:4000",
        api_key="secret",
    )

    result = await llm_client.call_llm(
        http_client,
        "Summarize this.",
        options=llm_client.ChatCompletionOptions(
            model="fast",
            max_tokens=123,
            temperature=0.2,
        ),
        config=config,
    )

    assert result == {"answer": "ok"}
    http_client.post.assert_awaited_once_with(
        "http://litellm.test:4000/v1/chat/completions",
        json={
            "model": "fast",
            "messages": [{"role": "user", "content": "Summarize this."}],
            "max_tokens": 123,
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        },
        headers={"Authorization": "Bearer secret"},
        timeout=120.0,
    )


@pytest.mark.asyncio
async def test_call_llm_strips_think_blocks(monkeypatch):
    """call_llm should remove thinking-model markup before JSON parsing."""
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {
        "choices": [
            {"message": {"content": '<think>hidden</think>\n{"answer":"clean"}'}}
        ]
    }
    http_client = AsyncMock()
    http_client.post.return_value = response

    result = await llm_client.call_llm(
        http_client,
        "Summarize this.",
        config=llm_client.LiteLLMConfig(
            base_url="http://litellm.test:4000",
            api_key="",
        ),
    )

    assert result == {"answer": "clean"}


def test_get_litellm_config_uses_fallback_env(monkeypatch):
    """Fallback auth envs should be used when the primary key is unset."""
    monkeypatch.delenv("LITELLM_API_KEY", raising=False)
    monkeypatch.setenv("LITELLM_MASTER_KEY", "master-secret")
    monkeypatch.setenv("LITELLM_BASE_URL", "http://litellm.test:4000")

    config = llm_client.get_litellm_config(
        primary_env_name="LITELLM_API_KEY",
        fallback_env_names=("LITELLM_MASTER_KEY",),
    )

    assert config == llm_client.LiteLLMConfig(
        base_url="http://litellm.test:4000",
        api_key="master-secret",
    )


def test_get_litellm_config_defaults_to_primary_key_only(monkeypatch):
    """Default config resolution should not widen fallback behavior for other callers."""
    monkeypatch.delenv("LITELLM_API_KEY", raising=False)
    monkeypatch.setenv("LITELLM_MASTER_KEY", "master-secret")
    monkeypatch.setenv("LITELLM_BASE_URL", "http://litellm.test:4000")

    config = llm_client.get_litellm_config()

    assert config == llm_client.LiteLLMConfig(
        base_url="http://litellm.test:4000",
        api_key="",
    )


def test_build_litellm_headers_omits_auth_without_key():
    """Empty API keys should not produce an Authorization header."""
    config = llm_client.LiteLLMConfig(
        base_url="http://litellm.test:4000",
        api_key="",
    )

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
        config=llm_client.LiteLLMConfig(
            base_url="http://litellm.test:4000",
            api_key="",
        ),
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
async def test_call_llm_json_value_parses_array_payload():
    """call_llm_json_value should support non-object JSON payloads."""
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {
        "choices": [{"message": {"content": '<think>ignore</think>\n["a", "b"]'}}]
    }
    http_client = AsyncMock()
    http_client.post.return_value = response

    result = await llm_client.call_llm_json_value(
        http_client,
        "Decompose this.",
        options=llm_client.ChatCompletionOptions(),
        config=llm_client.LiteLLMConfig(
            base_url="http://litellm.test:4000",
            api_key="",
        ),
    )

    assert result == ["a", "b"]


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
        config=llm_client.LiteLLMConfig(
            base_url="http://litellm.test:4000",
            api_key="",
        ),
    )

    assert result == [[1.0], [2.0]]
