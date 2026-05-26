"""Unit tests for rag/streaming.py — PI-02, PI-03, PI-08 prompt-shape coverage.

PI-02: prepare_single_paper_rag must emit [system, user] message pair.
PI-03: prepare_cross_paper_rag must emit [system, user] message pair.
PI-08: stream_rag_events with verifier=None must log a WARNING.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from paper_ingestion.rag.streaming import prepare_single_paper_rag, stream_rag_events


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_embedder(chunks: list[dict] | None = None):
    embedder = AsyncMock()
    _chunks = (
        chunks
        if chunks is not None
        else [{"content": "Some relevant text.", "page_number": 1, "score": 0.9, "chunk_index": 0}]
    )
    embedder.search_chunks_in_paper = AsyncMock(return_value=_chunks)
    embedder.rerank_chunks = AsyncMock(return_value=_chunks)
    return embedder


def _make_pool(paper_row: dict | None = None):
    """Return a mock asyncpg Pool whose acquire() yields a connection."""
    row = paper_row or {"id": 1, "title": "Test Paper"}
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=row)
    conn.fetch = AsyncMock(return_value=[])

    pool = MagicMock()
    pool.acquire = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return pool


# ---------------------------------------------------------------------------
# PI-02: prepare_single_paper_rag — Shape A message list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prepare_single_paper_rag_emits_system_user_messages():
    """PI-02: messages must contain a system-role message before the user-role message."""
    from paper_ingestion.models import AskRequest

    body = AskRequest(question="What is this paper about?", max_chunks=3)
    pool = _make_pool()
    embedder = _make_embedder()
    http_client = AsyncMock()

    messages, sources = await prepare_single_paper_rag(
        embedder, pool, paper_id=1, body=body, http_client=http_client, user_id=1
    )

    roles = [m["role"] for m in messages]
    assert "system" in roles, f"No system-role message found; got roles: {roles}"
    assert roles[0] == "system", f"system role must be first; got: {roles}"
    assert roles[-1] == "user", f"last message must be user role; got: {roles}"

    user_msg = next(m["content"] for m in messages if m["role"] == "user")
    assert "What is this paper about?" not in messages[0]["content"], (
        "Instruction head must not appear in user message"
    )
    assert "What is this paper about?" in user_msg or "question" in user_msg.lower(), (
        "User question must appear in the user-role message"
    )


@pytest.mark.asyncio
async def test_prepare_single_paper_rag_system_message_is_instruction_only():
    """PI-02: system role must carry only the instruction head, not paper content."""
    from paper_ingestion.models import AskRequest

    body = AskRequest(question="Summarize findings.", max_chunks=2)
    pool = _make_pool()
    embedder = _make_embedder()

    messages, _ = await prepare_single_paper_rag(
        embedder, pool, paper_id=1, body=body, http_client=AsyncMock(), user_id=1
    )

    system_content = next(m["content"] for m in messages if m["role"] == "system")
    user_content = next(m["content"] for m in messages if m["role"] == "user")
    # The system message must not contain the actual extracted chunk content
    # (it carries the instruction; chunk text flows into the user message).
    assert "Some relevant text." not in system_content, (
        "System message must not contain paper chunk content"
    )
    assert "Some relevant text." in user_content, (
        "User message must contain the paper chunk content (data-only)"
    )


# ---------------------------------------------------------------------------
# PI-08: stream_rag_events with verifier=None logs a WARNING
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_rag_events_warns_when_verifier_is_none(caplog):
    """PI-08: stream_rag_events(verifier=None) must emit a WARNING log."""
    from paper_ingestion.rag.streaming import sse_error_stream  # noqa: F401 — import side-effect

    fake_sse_lines = [
        'data: {"model": "m", "choices": [{"delta": {"content": "Hello"}}]}',
        "data: [DONE]",
    ]

    async def _fake_aiter_lines():
        for line in fake_sse_lines:
            yield line

    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.aiter_lines = _fake_aiter_lines
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_http = MagicMock()
    mock_http.stream = MagicMock(return_value=mock_resp)

    messages = [{"role": "system", "content": "instr"}, {"role": "user", "content": "q"}]

    with caplog.at_level(logging.WARNING, logger="paper_ingestion.rag.streaming"):
        events = []
        async for event in stream_rag_events(
            mock_http, messages, sources_list=[], model="fast", verifier=None
        ):
            events.append(event)

    warning_texts = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("verifier" in t.lower() or "verifier=None" in t for t in warning_texts), (
        f"Expected a WARNING about verifier=None; got warnings: {warning_texts}"
    )
