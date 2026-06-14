"""Unit tests for rag/decomposition.py — prompt-shape coverage.

decompose_query must use Shape A: system carries the decomposition rubric,
user message wraps the question via wrap_delimited("user_question", ...).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


# ---------------------------------------------------------------------------
# decompose_query — Shape A prompt split
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decompose_query_passes_system_prompt_in_options():
    """call_llm_structured must receive ChatCompletionOptions(system=...) non-empty."""
    from pydantic import RootModel

    captured_calls: list[dict] = []

    async def _fake_call_llm_structured(client, *, response_model, prompt, options, **kwargs):
        captured_calls.append({"prompt": prompt, "options": options})
        return RootModel[list[str]](root=["sub-query A", "sub-query B"])

    mock_client = AsyncMock()

    with (
        patch("paper_ingestion.rag.decomposition.call_llm_structured", _fake_call_llm_structured),
        patch("paper_ingestion.rag.decomposition.svc", create=True),
    ):
        from paper_ingestion.rag.decomposition import decompose_query

        result = await decompose_query(
            "How does attention relate to transformers?",
            model="fast",
            openai_client=mock_client,
        )

    assert result == ["sub-query A", "sub-query B"]
    assert captured_calls, "call_llm_structured was never called"

    opts = captured_calls[0]["options"]
    assert opts.system, "ChatCompletionOptions.system must be non-empty (Shape A)"

    prompt_text = captured_calls[0]["prompt"]
    assert "user_question" in prompt_text, (
        "User message must wrap the question with wrap_delimited('user_question', ...)"
    )


@pytest.mark.asyncio
async def test_decompose_query_user_message_contains_question():
    """The user-role prompt must contain the raw question text."""
    from pydantic import RootModel

    captured_calls: list[dict] = []

    async def _fake_call_llm_structured(client, *, response_model, prompt, options, **kwargs):
        captured_calls.append({"prompt": prompt, "options": options})
        return RootModel[list[str]](root=["q"])

    mock_client = AsyncMock()
    question = "What are the limitations of BERT?"

    with patch("paper_ingestion.rag.decomposition.call_llm_structured", _fake_call_llm_structured):
        from paper_ingestion.rag.decomposition import decompose_query

        await decompose_query(question, model="fast", openai_client=mock_client)

    prompt_text = captured_calls[0]["prompt"]
    assert question in prompt_text, (
        f"User message must embed the raw question; got: {prompt_text!r}"
    )

    system_text = captured_calls[0]["options"].system
    assert question not in system_text, (
        "System prompt must NOT contain the raw question (instruction-only)"
    )


@pytest.mark.asyncio
async def test_decompose_query_fallback_on_exception():
    """decompose_query must fall back to [question] on any LLM error."""

    async def _raise(*args, **kwargs):
        raise RuntimeError("LLM unavailable")

    mock_client = AsyncMock()
    question = "Fallback test question?"

    with patch("paper_ingestion.rag.decomposition.call_llm_structured", _raise):
        from paper_ingestion.rag.decomposition import decompose_query

        result = await decompose_query(question, model="fast", openai_client=mock_client)

    assert result == [question], f"Expected fallback to [question]; got {result}"
