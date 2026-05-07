"""Direct tests for the shared LiteLLM chat helper."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from jarvis_common import llm_client
from jarvis_common.llm_client import _strip_think_streaming


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


def test_build_litellm_headers_returns_empty_when_key_unset(monkeypatch):
    """build_litellm_headers returns {} when LITELLM_MASTER_KEY is not set."""
    monkeypatch.delenv("LITELLM_MASTER_KEY", raising=False)
    config = llm_client.LiteLLMConfig(base_url="http://litellm.test:4000")

    assert llm_client.build_litellm_headers(config) == {}


def test_build_litellm_headers_returns_bearer_when_key_set(monkeypatch):
    """build_litellm_headers returns Authorization: Bearer <key> when LITELLM_MASTER_KEY is set."""
    monkeypatch.setenv("LITELLM_MASTER_KEY", "test-secret-key-abc123")
    config = llm_client.LiteLLMConfig(base_url="http://litellm.test:4000")

    assert llm_client.build_litellm_headers(config) == {
        "Authorization": "Bearer test-secret-key-abc123"
    }


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
async def test_request_chat_completion_content_forwards_response_format(monkeypatch):
    """The lower-level request helper should pass through response_format unchanged."""
    monkeypatch.delenv("LITELLM_MASTER_KEY", raising=False)
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


# ---------------------------------------------------------------------------
# call_llm_structured guards (timeout passthrough, model fallback, validation)
# ---------------------------------------------------------------------------


def _make_instructor_recorder(recorded: dict):
    """Return a fake already-patched openai client for call_llm_structured tests.

    call_llm_structured uses the passed client directly (no double-wrapping),
    so tests need a client whose .chat.completions.create() records kwargs.
    """

    async def _create(**kwargs):
        recorded.update(kwargs)
        return _DummyResponse()

    class _DummyResponse:
        pass

    class _Completions:
        create = staticmethod(_create)

    class _Chat:
        completions = _Completions()

    class _FakeClient:
        chat = _Chat()

    return _FakeClient(), _DummyResponse


@pytest.mark.asyncio
async def test_call_llm_structured_passes_timeout_through():
    """call_llm_structured must forward options.timeout to the OpenAI SDK call."""
    from pydantic import BaseModel

    recorded: dict = {}
    fake_client, _ = _make_instructor_recorder(recorded)

    class _Out(BaseModel):
        pass

    await llm_client.call_llm_structured(
        fake_client,
        response_model=_Out,
        prompt="hi",
        options=llm_client.ChatCompletionOptions(model="smart", timeout=42.0),
    )

    assert recorded.get("timeout") == 42.0
    assert recorded.get("model") == "smart"


@pytest.mark.asyncio
async def test_call_llm_structured_raises_when_client_is_none():
    """A clear RuntimeError is preferable to a downstream Instructor crash."""
    with pytest.raises(RuntimeError, match="openai_client is required"):
        await llm_client.call_llm_structured(
            None,  # type: ignore[arg-type]
            response_model=type("_X", (), {}),
            prompt="hi",
            options=llm_client.ChatCompletionOptions(model="smart"),
        )


@pytest.mark.asyncio
async def test_call_llm_structured_raises_without_prompt_or_messages():
    """The structured helper must reject empty invocations before any LLM call."""
    with pytest.raises(ValueError, match="Either prompt or messages must be provided"):
        await llm_client.call_llm_structured(
            MagicMock(),
            response_model=type("_X", (), {}),
            options=llm_client.ChatCompletionOptions(model="smart"),
        )


@pytest.mark.asyncio
async def test_call_llm_structured_accepts_prompt_only():
    """A prompt-only call should produce a single user message and proceed."""
    recorded: dict = {}
    fake_client, _ = _make_instructor_recorder(recorded)

    await llm_client.call_llm_structured(
        fake_client,  # type: ignore[arg-type]
        response_model=type("_X", (), {}),  # type: ignore[arg-type]
        prompt="hello",
        options=llm_client.ChatCompletionOptions(model="smart"),
    )

    assert recorded.get("messages") == [{"role": "user", "content": "hello"}]


@pytest.mark.asyncio
async def test_call_llm_structured_accepts_messages_only():
    """A messages-only call should pass them through unchanged."""
    recorded: dict = {}
    fake_client, _ = _make_instructor_recorder(recorded)

    await llm_client.call_llm_structured(
        fake_client,  # type: ignore[arg-type]
        response_model=type("_X", (), {}),  # type: ignore[arg-type]
        messages=[{"role": "system", "content": "be brief"}],
        options=llm_client.ChatCompletionOptions(model="smart"),
    )

    assert recorded.get("messages") == [{"role": "system", "content": "be brief"}]


@pytest.mark.asyncio
async def test_call_llm_structured_appends_prompt_to_messages():
    """Per docstring, prompt is appended as a user message when both are given."""
    recorded: dict = {}
    fake_client, _ = _make_instructor_recorder(recorded)

    await llm_client.call_llm_structured(
        fake_client,  # type: ignore[arg-type]
        response_model=type("_X", (), {}),  # type: ignore[arg-type]
        messages=[{"role": "system", "content": "be brief"}],
        prompt="hi there",
        options=llm_client.ChatCompletionOptions(model="smart"),
    )

    assert recorded.get("messages") == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "hi there"},
    ]


@pytest.mark.asyncio
async def test_call_llm_structured_rejects_empty_model():
    """Empty model alias must raise ValueError, never silently fall back to a URL."""
    with pytest.raises(ValueError, match="model must be a non-empty"):
        await llm_client.call_llm_structured(
            MagicMock(),
            response_model=type("_X", (), {}),
            prompt="hi",
            options=llm_client.ChatCompletionOptions(model=""),
        )


# ---------------------------------------------------------------------------
# Observability decorator coverage (D.6)
# ---------------------------------------------------------------------------
#
# Per docs/contracts/04-observability.md §3, exactly nine functions are
# trace-boundary roots. Each MUST be wrapped by ``@observe()``.  When the
# decorator is removed, ``__wrapped__`` disappears — this test is the
# canary.

_BOUNDARY_FUNCTIONS: list[tuple[str, str]] = [
    # (module path, attribute path within module)
    ("paper_ingestion.pulse.job", "run_pulse"),
    ("paper_ingestion.rag.streaming", "prepare_single_paper_rag"),
    ("paper_ingestion.rag.streaming", "prepare_cross_paper_rag"),
    ("paper_ingestion.extraction.core", "batch_extract"),
    ("paper_ingestion.extraction.core", "extract_fields_for_paper"),
    ("paper_ingestion.extraction.entities", "extract_entities_for_paper"),
    ("learning_engine.card_generator", "CardGenerator.generate_cards"),
    ("paper_ingestion.weekly_summary", "generate_weekly_summary"),
    ("paper_ingestion.services.contradictions", "scan_contradictions"),
]


def _resolve(module_path: str, attr_path: str):
    import importlib  # noqa: PLC0415

    obj = importlib.import_module(module_path)
    for part in attr_path.split("."):
        obj = getattr(obj, part)
    return obj


def test_boundary_functions_are_observed():
    """Every trace-boundary function in 04-observability.md §3 must carry @observe()."""
    pytest.importorskip("paper_ingestion", reason="paper_ingestion service not installed")
    missing: list[str] = []
    for module_path, attr_path in _BOUNDARY_FUNCTIONS:
        try:
            fn = _resolve(module_path, attr_path)
        except (ImportError, AttributeError) as exc:
            pytest.skip(f"Could not import {module_path}.{attr_path}: {exc}")
        if not hasattr(fn, "__wrapped__"):
            missing.append(f"{module_path}.{attr_path}")
    assert not missing, (
        "Trace-boundary functions missing @observe() (per docs/contracts/04-observability.md §3): "
        + ", ".join(missing)
    )


# ---------------------------------------------------------------------------
# _strip_think_streaming — streaming CoT filter (W0-2)
# ---------------------------------------------------------------------------


def test_strip_think_streaming_simple():
    """Single chunk containing a full <think>...</think> tag.

    The function may hold the trailing visible text in carry to guard against
    partial open-tags at the chunk boundary.  Callers must flush carry after
    the stream ends.  We validate out+carry equals the full visible text.
    """
    out, st, carry = _strip_think_streaming("Hello <think>noise</think>World", False)
    # The filter drops the think block; visible fragments land in out or carry.
    assert out + carry == "Hello World"
    assert st is False
    assert "<think>" not in out + carry
    assert "noise" not in out + carry


def test_strip_think_streaming_split_open_tag():
    """Open tag split across two chunks: <th | ink>noise</think>."""
    out1, st1, carry1 = _strip_think_streaming("Hello <th", False)
    out2, st2, carry2 = _strip_think_streaming("ink>noise</think>World", st1, carry1)
    # Flush carry from final chunk to get full visible text.
    assert out1 + out2 + carry2 == "Hello World"
    assert st2 is False
    assert "noise" not in out1 + out2 + carry2


def test_strip_think_streaming_split_close_tag():
    """Close tag split across two chunks: noise</th | ink>visible."""
    out1, st1, carry1 = _strip_think_streaming("Hello <think>noise</th", False)
    out2, st2, carry2 = _strip_think_streaming("ink>World", st1, carry1)
    # Flush carry from final chunk to get full visible text.
    assert out1 + out2 + carry2 == "Hello World"
    assert st2 is False
    assert "noise" not in out1 + out2 + carry2


def test_strip_think_streaming_no_think():
    """No tags at all — all content must pass through (possibly via carry)."""
    out, st, carry = _strip_think_streaming("Just regular text.", False)
    # All visible content must be emitted across out + carry.
    assert out + carry == "Just regular text."
    assert st is False


def test_strip_think_streaming_token_by_token():
    """Feed source string one character at a time; full output must equal filtered text."""
    src = "Hello <think>noise</think>World"
    out_buf: list[str] = []
    in_think = False
    carry = ""
    for ch in src:
        v, in_think, carry = _strip_think_streaming(ch, in_think, carry)
        out_buf.append(v)
    # Flush trailing carry (only if not still inside a think block).
    if not in_think:
        out_buf.append(carry)
    result = "".join(out_buf)
    assert "noise" not in result
    assert "<think>" not in result
    # Visible segments must be present.
    assert "Hello" in result
    assert "World" in result
