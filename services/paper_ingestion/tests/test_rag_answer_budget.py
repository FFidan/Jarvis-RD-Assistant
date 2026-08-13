"""Contracts for RAG thinking policy, answer budgets, and streamed hygiene codes."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from paper_ingestion.rag import streaming
from paper_ingestion.services import litellm_config
from paper_ingestion.services.litellm_api import LiteLLMDeployment


class _Pool:
    def __init__(self, row=None, *, error: Exception | None = None):
        self._conn = AsyncMock()
        if error is None:
            self._conn.fetchrow.return_value = row
        else:
            self._conn.fetchrow.side_effect = error
        self._ctx = AsyncMock()
        self._ctx.__aenter__.return_value = self._conn

    def acquire(self):
        return self._ctx


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("row", "expected"),
    [
        (None, litellm_config.ThinkingPreferenceState.ABSENT),
        ({"value": True}, litellm_config.ThinkingPreferenceState.EXPLICIT_DISABLED),
        ({"value": False}, litellm_config.ThinkingPreferenceState.EXPLICIT_ENABLED),
    ],
)
async def test_thinking_preference_has_distinct_persisted_states(row, expected):
    assert await litellm_config._get_thinking_disabled("qwen3:8b", "host-a", _Pool(row)) is expected


@pytest.mark.asyncio
async def test_thinking_preference_read_failure_is_not_collapsed_to_enabled():
    state = await litellm_config._get_thinking_disabled(
        "qwen3:8b", "host-a", _Pool(error=RuntimeError("database unavailable"))
    )
    assert state is litellm_config.ThinkingPreferenceState.READ_FAILED


def _deployment(model: str, *, think=...) -> LiteLLMDeployment:
    params: dict[str, object] = {"model": model}
    if think is not ...:
        params["think"] = think
    return LiteLLMDeployment(model_name="smart", litellm_params=params)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("deployment", "expected"),
    [
        (_deployment("ollama_chat/qwen3:8b", think=False), 700),
        (_deployment("ollama_chat/qwen3:8b"), 2800),
        (_deployment("ollama_chat/gemma3:12b"), 700),
    ],
)
async def test_answer_budget_tracks_current_routing(monkeypatch, deployment, expected):
    monkeypatch.setattr(
        streaming,
        "get_litellm_deployments",
        AsyncMock(return_value=[deployment]),
    )

    budget = await streaming.resolve_rag_answer_budget()

    assert budget.completion_tokens == expected
    assert budget.reserved_output_tokens == expected


@pytest.mark.asyncio
async def test_uncertain_routing_uses_thinking_budget(monkeypatch):
    monkeypatch.setattr(
        streaming,
        "get_litellm_deployments",
        AsyncMock(side_effect=RuntimeError("sidecar unavailable")),
    )
    assert (await streaming.resolve_rag_answer_budget()).completion_tokens == 2800


def test_answer_budget_rejects_prompt_request_divergence():
    with pytest.raises(ValueError, match="must match"):
        streaming.RagAnswerBudget(completion_tokens=700, reserved_output_tokens=2800)


def test_prompt_reservation_consumes_the_same_answer_budget(monkeypatch):
    seen: list[int] = []

    def budget_chars(_num_ctx: int, *, reserved_output_tokens: int) -> int:
        seen.append(reserved_output_tokens)
        return 10_000

    monkeypatch.setattr(streaming, "max_input_chars", budget_chars)
    answer_budget = streaming.RagAnswerBudget.for_thinking()

    input_char_budget = answer_budget.input_char_limit(8192)
    kept, content = streaming._fit_chunks_to_budget(
        [{"content": "source"}],
        lambda chunks: chunks[0]["content"],
        "system",
        [],
        input_char_budget,
    )

    assert kept == [{"content": "source"}]
    assert content == "source"
    assert seen == [answer_budget.completion_tokens]


@pytest.mark.asyncio
async def test_non_streaming_request_consumes_the_same_answer_budget(monkeypatch):
    from paper_ingestion._state import svc
    from paper_ingestion.routers import rag

    previous_client = svc.openai_client
    svc.openai_client = object()
    call = AsyncMock(return_value=object())
    monkeypatch.setattr(rag, "call_llm_structured", call)
    answer_budget = streaming.RagAnswerBudget.for_thinking()
    try:
        await rag._call_rag_llm(
            [{"role": "user", "content": "question"}],
            smart_model="smart",
            answer_budget=answer_budget,
        )
    finally:
        svc.openai_client = previous_client

    options = call.call_args.kwargs["options"]
    assert options.max_tokens == answer_budget.reserved_output_tokens


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "expected_code"),
    [
        ("<think>hidden only</think>", "llm_empty_visible_content"),
        ("Let me inspect the sources before answering.", "llm_visible_work_notes"),
    ],
)
async def test_streamed_hygiene_failures_have_stable_codes(content, expected_code):
    line = f'data: {{"choices": [{{"delta": {{"content": {json.dumps(content)}}}}}]}}'
    response = MagicMock()
    response.raise_for_status.return_value = None

    async def lines():
        yield line
        yield "data: [DONE]"

    response.aiter_lines = lines
    context = AsyncMock()
    context.__aenter__.return_value = response
    client = AsyncMock(spec=httpx.AsyncClient)
    client.stream.return_value = context

    events = [
        event
        async for event in streaming.stream_rag_events(
            client,
            [{"role": "user", "content": "question"}],
            [],
            answer_budget=streaming.RagAnswerBudget.for_thinking(),
        )
    ]

    error = json.loads(events[0].removeprefix("data: "))
    assert error == {
        "type": "error",
        "message": "The model did not return a usable final answer. Please try again.",
        "code": expected_code,
    }
    request_body = client.stream.call_args.kwargs["json"]
    assert request_body["max_tokens"] == 2800
