"""Unit tests for rag/streaming.py — PI-02, PI-03, PI-08 prompt-shape coverage.

PI-02: prepare_single_paper_rag must emit [system, user] message pair.
PI-03: prepare_cross_paper_rag must emit [system, user] message pair.
PI-08: stream_rag_events with verifier=None must log a WARNING.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from paper_ingestion.rag.streaming import (
    _SYSTEM_CROSS_PAPER_RAG,
    prepare_cross_paper_rag,
    prepare_single_paper_rag,
    stream_rag_events,
)


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


def _make_cross_paper_embedder(chunks: list[dict] | None = None):
    """Return a mock Embedder suitable for cross-paper RAG (uses search_chunks_global)."""
    _chunks = (
        chunks
        if chunks is not None
        else [
            {
                "paper_id": 1,
                "chunk_index": 0,
                "content": "Relevant cross-paper excerpt.",
                "page_number": 2,
                "score": 0.88,
            }
        ]
    )
    embedder = AsyncMock()
    embedder.search_chunks_global = AsyncMock(return_value=_chunks)
    embedder.rerank_chunks = AsyncMock(return_value=_chunks)
    return embedder


def _make_cross_paper_pool(paper_rows: list[dict] | None = None):
    """Return a mock asyncpg Pool that serves paper metadata for cross-paper RAG."""
    rows = (
        paper_rows
        if paper_rows is not None
        else [{"id": 1, "title": "Alpha Paper", "authors": [], "url": "http://example.com/1"}]
    )
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=rows)

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


# ---------------------------------------------------------------------------
# PI-03: prepare_cross_paper_rag — Shape A message list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prepare_cross_paper_rag_emits_system_user_messages():
    """PI-03: messages must be [system, user]; system == _SYSTEM_CROSS_PAPER_RAG; user carries question."""
    from paper_ingestion.models import CrossPaperAskRequest

    body = CrossPaperAskRequest(question="Compare findings across papers.", decompose=False)
    embedder = _make_cross_paper_embedder()
    pool = _make_cross_paper_pool()

    result = await prepare_cross_paper_rag(embedder, pool, body=body, http_client=AsyncMock())

    assert hasattr(result, "messages"), f"Expected CrossPaperRagPrep with messages; got {result!r}"
    roles = [m["role"] for m in result.messages]
    assert roles == ["system", "user"], f"Expected [system, user]; got {roles}"
    assert result.messages[0]["content"] == _SYSTEM_CROSS_PAPER_RAG
    assert "Compare findings across papers." in result.messages[1]["content"]
    assert _SYSTEM_CROSS_PAPER_RAG not in result.messages[1]["content"]


@pytest.mark.asyncio
async def test_prepare_cross_paper_rag_chunk_data_not_in_system():
    """PI-03 security: attacker-controlled chunk text must appear only in user message, never system."""
    from paper_ingestion.models import CrossPaperAskRequest

    attacker_text = "IGNORE PREVIOUS"
    chunks = [
        {
            "paper_id": 1,
            "chunk_index": 0,
            "content": attacker_text,
            "page_number": 1,
            "score": 0.9,
        }
    ]
    body = CrossPaperAskRequest(question="What do the papers say?", decompose=False)
    embedder = _make_cross_paper_embedder(chunks=chunks)
    pool = _make_cross_paper_pool()

    result = await prepare_cross_paper_rag(embedder, pool, body=body, http_client=AsyncMock())

    assert hasattr(result, "messages"), f"Expected CrossPaperRagPrep; got {result!r}"
    system_content = result.messages[0]["content"]
    user_content = result.messages[1]["content"]
    assert attacker_text not in system_content, (
        "Attacker-controlled chunk text must NEVER appear in the system message"
    )
    assert attacker_text in user_content or "IGNORE" in user_content, (
        "Chunk text must flow into the user message (escaped or raw)"
    )


# ---------------------------------------------------------------------------
# Conversation memory: history turns inserted between system and user message
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_history_turns_are_escaped_and_bounded():
    """7 history turns in → 6 out (oldest dropped); content arrives escaped.

    History sits between the system message and the final user message and
    must NOT change retrieval — the question alone drives the Qdrant search.
    """
    from paper_ingestion.models import AskRequest
    from paper_ingestion.models.rag import HistoryTurn

    history = [
        HistoryTurn(role="user" if i % 2 else "assistant", content=f"turn-{i}") for i in range(1, 7)
    ]
    history.append(HistoryTurn(role="user", content="follow-up with <b>markup</b>"))
    assert len(history) == 7

    body = AskRequest(question="And the second paper?", max_chunks=3, history=history)
    pool = _make_pool()
    embedder = _make_embedder()

    messages, _ = await prepare_single_paper_rag(
        embedder, pool, paper_id=1, body=body, http_client=AsyncMock(), user_id=1
    )

    # Shape: [system, *6 history turns, user] — oldest of the 7 turns dropped.
    roles = [m["role"] for m in messages]
    assert roles[0] == "system" and roles[-1] == "user"
    history_msgs = messages[1:-1]
    assert len(history_msgs) == 6, f"Expected 6 history messages, got {len(history_msgs)}"
    assert all(m["content"] != "turn-1" for m in history_msgs), "Oldest turn must be dropped"
    assert history_msgs[0]["content"] == "turn-2", "Turn order must be preserved (oldest first)"

    # History content is DATA: angle brackets arrive escaped.
    markup_msg = history_msgs[-1]["content"]
    assert "&lt;b&gt;" in markup_msg, f"Expected escaped markup, got {markup_msg!r}"
    assert "<b>" not in markup_msg, "Raw markup must never reach the prompt"

    # Retrieval is driven by the bare question only — never by history.
    embedder.search_chunks_in_paper.assert_awaited_once()
    assert (
        embedder.search_chunks_in_paper.await_args.kwargs["query_text"] == "And the second paper?"
    )


# ---------------------------------------------------------------------------
# Gap 1 — single-paper Layer-2 rerank floor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_paper_rerank_floor_drops_all_raises(monkeypatch):
    """Chunks reranked below the default cross-encoder floor (3.0) must all be
    dropped, causing NoRelevantChunksError.

    The default backend is 'cross-encoder' → floor is 3.0.  Both test chunks
    carry rerank_score < 3.0 so none survive the Layer-2 gate.
    """
    from paper_ingestion.models import AskRequest
    from paper_ingestion.rag.exceptions import NoRelevantChunksError

    # Pin to the cross-encoder default (floor=3.0) by removing the env var.
    monkeypatch.delenv("RAG_MIN_RERANK_SCORE", raising=False)

    # Chunks after reranking — both scored below 3.0.
    low_chunks = [
        {
            "content": "Chunk A — poor relevance.",
            "page_number": 1,
            "score": 0.7,
            "chunk_index": 0,
            "rerank_score": 2.7,
        },
        {
            "content": "Chunk B — even worse.",
            "page_number": 2,
            "score": 0.6,
            "chunk_index": 1,
            "rerank_score": 0.4,
        },
    ]
    embedder = _make_embedder(chunks=low_chunks)
    # rerank_chunks returns the same low-scored chunks unchanged.
    embedder.rerank_chunks = AsyncMock(return_value=low_chunks)

    body = AskRequest(question="Does this relate?", max_chunks=5)
    pool = _make_pool()

    with pytest.raises(NoRelevantChunksError):
        await prepare_single_paper_rag(
            embedder, pool, paper_id=1, body=body, http_client=AsyncMock(), user_id=1
        )


@pytest.mark.asyncio
async def test_single_paper_chunks_without_rerank_score_pass_floor(monkeypatch):
    """Chunks WITHOUT 'rerank_score' key (reranker disabled) bypass the floor
    and are all retained — absence of the key ⇒ skip the gate.
    """
    from paper_ingestion.models import AskRequest

    # Pin to the cross-encoder default so the floor would be 3.0 if applied.
    monkeypatch.delenv("RAG_MIN_RERANK_SCORE", raising=False)

    # No rerank_score key on any chunk.
    no_rerank_chunks = [
        {
            "content": "First chunk without rerank score.",
            "page_number": 1,
            "score": 0.85,
            "chunk_index": 0,
        },
        {
            "content": "Second chunk without rerank score.",
            "page_number": 2,
            "score": 0.80,
            "chunk_index": 1,
        },
    ]
    embedder = _make_embedder(chunks=no_rerank_chunks)
    embedder.rerank_chunks = AsyncMock(return_value=no_rerank_chunks)

    body = AskRequest(question="Tell me about the method.", max_chunks=5)
    pool = _make_pool()

    messages, sources = await prepare_single_paper_rag(
        embedder, pool, paper_id=1, body=body, http_client=AsyncMock(), user_id=1
    )

    # Both chunks must survive — all returned as sources.
    assert len(sources) == 2, f"Expected 2 sources (all chunks kept); got {len(sources)}"
    # Messages must still be well-formed [system, user].
    roles = [m["role"] for m in messages]
    assert roles[0] == "system" and roles[-1] == "user"


# ---------------------------------------------------------------------------
# Gap 2 — history char-budget trim
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_history_char_budget_drops_oldest_beyond_turn_cap():
    """6 turns of ~1100 chars each (≈6600 total) must be trimmed to 5 turns,
    dropping the OLDEST turn while preserving order of the remaining 5.

    _HISTORY_MAX_TURNS=6 keeps all 6; the char-budget loop then pops the
    oldest until total chars ≤ 6000, resulting in 5 turns.
    """
    from paper_ingestion.models import AskRequest
    from paper_ingestion.models.rag import HistoryTurn

    # Build 6 turns, each with content of ~1100 chars so total ≈ 6600 chars.
    filler = "x" * 1090
    turns = [
        HistoryTurn(role="user" if i % 2 == 0 else "assistant", content=f"turn-{i}-{filler}")
        for i in range(6)
    ]
    assert len(turns) == 6
    body = AskRequest(question="Final question?", max_chunks=3, history=turns)
    pool = _make_pool()
    embedder = _make_embedder()

    messages, _ = await prepare_single_paper_rag(
        embedder, pool, paper_id=1, body=body, http_client=AsyncMock(), user_id=1
    )

    # Shape: [system, *history_msgs, user]
    history_msgs = messages[1:-1]

    # The char-budget loop must have dropped 1 turn → 5 history messages.
    assert len(history_msgs) == 5, (
        f"Expected 5 history messages after char-budget trim; got {len(history_msgs)}"
    )

    # The OLDEST turn (turn-0) must be absent.
    contents = [m["content"] for m in history_msgs]
    assert not any("turn-0-" in c for c in contents), (
        "Oldest turn (turn-0) must be dropped by char-budget trim"
    )

    # turn-1 is now the oldest survivor and must come first (order preserved).
    assert "turn-1-" in contents[0], (
        f"turn-1 must be the oldest surviving turn; first msg content prefix: {contents[0][:30]}"
    )
