"""Unit tests for rag/streaming.py — prompt-shape coverage.

prepare_single_paper_rag must emit [system, user] message pair.
prepare_cross_paper_rag must emit [system, user] message pair.
stream_rag_events with verifier=None must log a WARNING.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from jarvis_common.testing import make_pool_and_conn

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


_STORED_CHUNK_INDEXES = range(4)


def _make_cross_paper_pool(paper_rows: list[dict] | None = None):
    """Return a mock asyncpg Pool that serves paper metadata for cross-paper RAG.

    The paper_chunks lookup answers that each requested paper stores its first
    few chunk indexes, standing in for a fully processed corpus.
    """
    rows = (
        paper_rows
        if paper_rows is not None
        else [{"id": 1, "title": "Alpha Paper", "authors": [], "url": "http://example.com/1"}]
    )

    async def _fetch(sql, *args):
        if "paper_chunks" not in sql:
            return rows
        return [
            {"paper_id": paper_id, "chunk_index": index}
            for paper_id in args[0]
            for index in _STORED_CHUNK_INDEXES
        ]

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(side_effect=_fetch)

    pool = MagicMock()
    pool.acquire = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return pool


def _pool_storing_chunk_keys(
    stored_keys: list[tuple[int, int]],
    *,
    paper_row: dict | None = None,
    paper_rows: list[dict] | None = None,
):
    """Return a pool whose ``paper_chunks`` lookup answers with exactly *stored_keys*.

    ``paper_row`` serves the single-paper title read and ``paper_rows`` the
    cross-paper metadata read; the caller-library read answers empty, which is
    what a reader with no private memberships looks like.
    """
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=paper_row)

    async def _fetch(sql, *_args):
        if "paper_chunks" in sql:
            return [{"paper_id": pid, "chunk_index": index} for pid, index in stored_keys]
        return paper_rows or []

    conn.fetch = AsyncMock(side_effect=_fetch)
    pool, _ = make_pool_and_conn(conn=conn)
    return pool


def _echoing_rerank():
    """Rerank double returning what it was handed, so an earlier drop stays visible."""
    return AsyncMock(side_effect=lambda _question, candidates, top_k: candidates[:top_k])


# ---------------------------------------------------------------------------
# prepare_single_paper_rag — Shape A message list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prepare_single_paper_rag_emits_system_user_messages():
    """Messages must contain a system-role message before the user-role message."""
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
    """System role must carry only the instruction head, not paper content."""
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
# stream_rag_events with verifier=None logs a WARNING
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_rag_events_warns_when_verifier_is_none(caplog):
    """stream_rag_events(verifier=None) must emit a WARNING log."""
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
# prepare_cross_paper_rag — Shape A message list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prepare_cross_paper_rag_emits_system_user_messages():
    """Messages must be [system, user]; system == _SYSTEM_CROSS_PAPER_RAG; user carries question."""
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
    """Attacker-controlled chunk text must appear only in user message, never system."""
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


# ---------------------------------------------------------------------------
# Context-window char budget: tail chunks dropped so prompt + answer fit
# ---------------------------------------------------------------------------


def _prompt_budget() -> int:
    from jarvis_common.prompt_safety import max_input_chars
    from jarvis_common.settings import get_core_settings
    from paper_ingestion.rag.streaming import _ANSWER_MAX_TOKENS

    return max_input_chars(
        get_core_settings().llm_smart_num_ctx, reserved_output_tokens=_ANSWER_MAX_TOKENS
    )


@pytest.mark.asyncio
async def test_single_paper_oversized_chunks_dropped_to_fit_budget(monkeypatch):
    """Oversized chunk sets must lose tail chunks until the prompt fits the
    char budget, with sources staying 1:1 with the chunks in the prompt."""
    from paper_ingestion.models import AskRequest

    monkeypatch.setenv("LLM_SMART_NUM_CTX", "1000")
    chunks = [
        {
            "content": f"marker-{i}-" + "x" * 1200,
            "page_number": i + 1,
            "score": 0.9 - i * 0.01,
            "chunk_index": i,
        }
        for i in range(5)
    ]
    embedder = _make_embedder(chunks=chunks)
    body = AskRequest(question="What fits the window?", max_chunks=5)
    pool = _make_pool()

    messages, sources = await prepare_single_paper_rag(
        embedder, pool, paper_id=1, body=body, http_client=AsyncMock(), user_id=1
    )

    assert 1 <= len(sources) < 5, f"Expected tail chunks dropped; got {len(sources)} sources"
    total = sum(len(m["content"]) for m in messages)
    assert total <= _prompt_budget(), f"Prompt ({total} chars) exceeds budget {_prompt_budget()}"

    # Tail-drop: surviving sources are the head of the original order.
    assert [s["content"] for s in sources] == [c["content"] for c in chunks[: len(sources)]]

    # 1:1 invariant: every prompt chunk is a source and vice versa.
    user_content = messages[-1]["content"]
    for i, c in enumerate(chunks):
        in_prompt = f"marker-{i}-" in user_content
        in_sources = any(s["content"] == c["content"] for s in sources)
        assert in_prompt == in_sources, f"chunk {i}: in_prompt={in_prompt} in_sources={in_sources}"


@pytest.mark.asyncio
async def test_single_paper_under_budget_keeps_all_chunks():
    """Under the char budget, every chunk reaches both prompt and sources."""
    from paper_ingestion.models import AskRequest

    chunks = [
        {"content": f"small-{i}", "page_number": i + 1, "score": 0.9, "chunk_index": i}
        for i in range(3)
    ]
    embedder = _make_embedder(chunks=chunks)
    body = AskRequest(question="Short question.", max_chunks=5)
    pool = _make_pool()

    messages, sources = await prepare_single_paper_rag(
        embedder, pool, paper_id=1, body=body, http_client=AsyncMock(), user_id=1
    )

    assert [s["content"] for s in sources] == [c["content"] for c in chunks]
    user_content = messages[-1]["content"]
    assert all(f"small-{i}" in user_content for i in range(3))


@pytest.mark.asyncio
async def test_cross_paper_oversized_chunks_dropped_to_fit_budget(monkeypatch):
    """Worst-case cross-paper assembly must fit the window; sources mirror the
    post-drop chunk list exactly."""
    from paper_ingestion.models import CrossPaperAskRequest

    monkeypatch.setenv("LLM_SMART_NUM_CTX", "1000")
    chunks = [
        {
            "paper_id": pid,
            "chunk_index": ci,
            "content": f"marker-{pid}-{ci}-" + "x" * 1200,
            "page_number": ci + 1,
            "score": 0.90 - (pid * 2 + ci) * 0.005,
        }
        for pid in (1, 2, 3)
        for ci in (0, 1)
    ]
    paper_rows = [
        {"id": pid, "title": f"Paper {pid}", "authors": [], "url": f"http://example.com/{pid}"}
        for pid in (1, 2, 3)
    ]
    embedder = _make_cross_paper_embedder(chunks=chunks)
    pool = _make_cross_paper_pool(paper_rows=paper_rows)
    body = CrossPaperAskRequest(
        question="Compare all of it.", decompose=False, max_chunks=6, max_papers=3
    )

    result = await prepare_cross_paper_rag(embedder, pool, body=body, http_client=AsyncMock())

    assert hasattr(result, "messages"), f"Expected CrossPaperRagPrep; got {result!r}"
    assert 1 <= len(result.sources) < 6, (
        f"Expected tail chunks dropped; got {len(result.sources)} sources"
    )
    total = sum(len(m["content"]) for m in result.messages)
    assert total <= _prompt_budget(), f"Prompt ({total} chars) exceeds budget {_prompt_budget()}"

    user_content = result.messages[-1]["content"]
    source_contents = {s["content"] for s in result.sources}
    for c in chunks:
        marker = c["content"][: c["content"].index("x")]
        in_prompt = marker in user_content
        in_sources = c["content"] in source_contents
        assert in_prompt == in_sources, f"{marker}: in_prompt={in_prompt} in_sources={in_sources}"


@pytest.mark.asyncio
async def test_cross_paper_under_budget_keeps_all_chunks():
    """Under the char budget, the cross-paper prompt and sources are untouched."""
    from paper_ingestion.models import CrossPaperAskRequest

    chunks = [
        {
            "paper_id": pid,
            "chunk_index": 0,
            "content": f"small-{pid}",
            "page_number": 1,
            "score": 0.9 - pid * 0.01,
        }
        for pid in (1, 2)
    ]
    paper_rows = [
        {"id": pid, "title": f"Paper {pid}", "authors": [], "url": f"http://example.com/{pid}"}
        for pid in (1, 2)
    ]
    embedder = _make_cross_paper_embedder(chunks=chunks)
    pool = _make_cross_paper_pool(paper_rows=paper_rows)
    body = CrossPaperAskRequest(question="Tiny question.", decompose=False)

    result = await prepare_cross_paper_rag(embedder, pool, body=body, http_client=AsyncMock())

    assert hasattr(result, "messages"), f"Expected CrossPaperRagPrep; got {result!r}"
    assert {s["content"] for s in result.sources} == {c["content"] for c in chunks}
    user_content = result.messages[-1]["content"]
    assert all(f"small-{pid}" in user_content for pid in (1, 2))


# ---------------------------------------------------------------------------
# Excerpt liveness: an excerpt reaches the caller only while a stored
# paper_chunks row still backs it.
# ---------------------------------------------------------------------------

_STORED_EXCERPT = "Text the paper still stores."
_UNBACKED_EXCERPT = "Text no stored chunk row backs."


@pytest.mark.asyncio
async def test_single_paper_rag_drops_an_excerpt_with_no_stored_chunk_row():
    """A retrieved excerpt the paper no longer stores reaches neither prompt nor sources."""
    from paper_ingestion.models import AskRequest

    stored = {"content": _STORED_EXCERPT, "page_number": 1, "score": 0.90, "chunk_index": 0}
    unbacked = {"content": _UNBACKED_EXCERPT, "page_number": 2, "score": 0.95, "chunk_index": 1}
    embedder = _make_embedder(chunks=[stored, unbacked])
    embedder.rerank_chunks = _echoing_rerank()
    pool = _pool_storing_chunk_keys([(1, 0)], paper_row={"id": 1, "title": "Test Paper"})

    messages, sources = await prepare_single_paper_rag(
        embedder,
        pool,
        paper_id=1,
        body=AskRequest(question="What does the paper say?", max_chunks=5),
        http_client=AsyncMock(),
        user_id=1,
    )

    assert [s["content"] for s in sources] == [_STORED_EXCERPT]
    assert _UNBACKED_EXCERPT not in messages[-1]["content"]


@pytest.mark.asyncio
async def test_cross_paper_rag_drops_an_excerpt_with_no_stored_chunk_row():
    """One paper losing its chunk rows costs only that paper's excerpt."""
    from paper_ingestion.models import CrossPaperAskRequest

    stored = {
        "paper_id": 1,
        "chunk_index": 0,
        "content": _STORED_EXCERPT,
        "page_number": 1,
        "score": 0.90,
    }
    unbacked = {
        "paper_id": 2,
        "chunk_index": 0,
        "content": _UNBACKED_EXCERPT,
        "page_number": 1,
        "score": 0.95,
    }
    embedder = _make_cross_paper_embedder(chunks=[stored, unbacked])
    embedder.rerank_chunks = _echoing_rerank()
    pool = _pool_storing_chunk_keys(
        [(1, 0)],
        paper_rows=[
            {"id": 1, "title": "Alpha Paper", "authors": [], "url": "http://example.com/1"},
            {"id": 2, "title": "Beta Paper", "authors": [], "url": "http://example.com/2"},
        ],
    )

    result = await prepare_cross_paper_rag(
        embedder,
        pool,
        body=CrossPaperAskRequest(question="What do the papers say?", decompose=False),
        http_client=AsyncMock(),
    )

    assert hasattr(result, "sources"), f"Expected CrossPaperRagPrep; got {result!r}"
    assert [s["content"] for s in result.sources] == [_STORED_EXCERPT]
    assert _UNBACKED_EXCERPT not in result.messages[-1]["content"]


@pytest.mark.asyncio
async def test_single_paper_rag_drops_an_excerpt_carrying_no_chunk_index():
    """``chunk_index`` is NOT NULL in ``paper_chunks``, so a chunk without one matches nothing."""
    from paper_ingestion.models import AskRequest

    stored = {"content": _STORED_EXCERPT, "page_number": 1, "score": 0.90, "chunk_index": 0}
    index_missing = {"content": "Text retrieved without an index.", "page_number": 2, "score": 0.95}
    index_null = {
        **index_missing,
        "content": "Text retrieved with a null index.",
        "chunk_index": None,
    }
    embedder = _make_embedder(chunks=[stored, index_missing, index_null])
    embedder.rerank_chunks = _echoing_rerank()
    pool = _pool_storing_chunk_keys([(1, 0)], paper_row={"id": 1, "title": "Test Paper"})

    _messages, sources = await prepare_single_paper_rag(
        embedder,
        pool,
        paper_id=1,
        body=AskRequest(question="What does the paper say?", max_chunks=5),
        http_client=AsyncMock(),
        user_id=1,
    )

    assert [s["content"] for s in sources] == [_STORED_EXCERPT]
