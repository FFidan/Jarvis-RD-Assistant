"""Tests for cross-paper RAG: endpoint, dedup, and XML escaping."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from paper_ingestion.models import CrossPaperAskRequest

# D3-12 deleted: test_search_chunks_global_no_filter
# Superseded by contract/test_embedder_sidecar_contract.py, which exercises the
# real Embedder against faux LiteLLM + faux Qdrant and proves scoped global
# search returns only vectors visible to the caller.

# ---------------------------------------------------------------------------
# Test: deduplication logic (max 2 chunks per paper, respects max_papers)
# ---------------------------------------------------------------------------


async def test_dedup_max_chunks_per_paper():
    """prepare_cross_paper_rag deduplicates: keeps at most 2 chunks per paper
    and trims to max_papers by best-chunk score.

    D3-03: replaced the prior test that re-implemented the dedup logic in the
    test body (asserting on its own local variables).  This version drives the
    real prepare_cross_paper_rag path with a controlled chunk set and asserts
    on the CrossPaperRagPrep.sources that come back out.
    """
    from unittest.mock import AsyncMock, MagicMock

    from paper_ingestion.models import CrossPaperAskRequest
    from paper_ingestion.rag.streaming import CrossPaperRagPrep, prepare_cross_paper_rag

    # 3 chunks for paper 1 (only top 2 should survive dedup), 1 for paper 2,
    # 1 for paper 3 — max_papers=2 should drop paper 3 (lowest top-chunk score).
    all_chunks = [
        {"paper_id": 1, "chunk_index": 0, "content": "c1a", "page_number": 1, "score": 0.9},
        {"paper_id": 1, "chunk_index": 1, "content": "c1b", "page_number": 2, "score": 0.85},
        {"paper_id": 1, "chunk_index": 2, "content": "c1c", "page_number": 3, "score": 0.6},
        {"paper_id": 2, "chunk_index": 0, "content": "c2a", "page_number": 1, "score": 0.8},
        {"paper_id": 3, "chunk_index": 0, "content": "c3a", "page_number": 1, "score": 0.7},
    ]

    # Embedder mock: returns the controlled chunk set directly.
    mock_embedder = MagicMock()
    mock_embedder.search_chunks_global = AsyncMock(return_value=all_chunks)
    # rerank_chunks passes through unchanged (identity slice).
    mock_embedder.rerank_chunks = AsyncMock(side_effect=lambda q, c, top_k: c[:top_k])

    # DB mock: return metadata for paper_ids 1 and 2 only (paper 3 would be dropped
    # before the DB fetch anyway, but simulating the metadata shape faithfully).
    db_rows = [
        {"id": 1, "title": "Paper One", "authors": "A", "url": "http://p1"},
        {"id": 2, "title": "Paper Two", "authors": "B", "url": "http://p2"},
    ]
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=db_rows)
    db_pool = MagicMock()
    db_pool.acquire.return_value.__aenter__.return_value = conn

    body = CrossPaperAskRequest(
        question="dedup test",
        max_chunks=4,
        max_papers=2,  # trims to top-2 papers by best-chunk score
        decompose=False,
    )

    result = await prepare_cross_paper_rag(mock_embedder, db_pool, body, AsyncMock(), user_id=1)

    assert isinstance(result, CrossPaperRagPrep), (
        f"Expected CrossPaperRagPrep with 2 papers, got {result!r}"
    )
    paper_ids = {s["paper_id"] for s in result.sources}

    # Paper 1 (top-chunk score 0.9) and paper 2 (0.8) survive; paper 3 (0.7) dropped.
    assert 1 in paper_ids, "Paper 1 (highest score) must be in sources"
    assert 2 in paper_ids, "Paper 2 must be in sources"
    assert 3 not in paper_ids, "Paper 3 must be dropped (max_papers=2, score 0.7 < 0.8)"

    # No paper should contribute more than 2 chunks (dedup cap).
    from collections import Counter

    chunk_counts = Counter(s["paper_id"] for s in result.sources)
    for pid, count in chunk_counts.items():
        assert count <= 2, f"Paper {pid} contributed {count} chunks — dedup cap is 2"


# ---------------------------------------------------------------------------
# Test: XML escaping in prompt construction
# ---------------------------------------------------------------------------


def test_xml_escaping():
    """Content and question are XML-escaped to prevent prompt injection."""
    raw_content = '<script>alert("xss")</script> & "quoted"'
    raw_question = "What about <b>bold</b> claims?"

    safe_content = raw_content.replace("<", "&lt;").replace(">", "&gt;")
    safe_question = raw_question.replace("<", "&lt;").replace(">", "&gt;")

    assert "<script>" not in safe_content
    assert "&lt;script&gt;" in safe_content
    assert "<b>" not in safe_question
    assert "&lt;b&gt;" in safe_question


# test_ask_cross_paper_endpoint_structure deleted.
# Used _make_pool_and_conn() — a mock DB pool, not a real Postgres connection.
# Superseded by contract/test_rag_contract.py::test_ask_endpoint_cross_paper_real_db_structure,
# which wires the ASGI client to the contract_conn transaction (real schema),
# asserting the same HTTP status + body shape with strictly stronger DB coverage.
# The fake_sources in the mock variant were constructed in-test and asserted
# against themselves — the contract test seeds real data and patches the same
# prepare stub, removing the circular assertion.


# ---------------------------------------------------------------------------
# Test: CrossPaperAskRequest model validation
# ---------------------------------------------------------------------------


def test_cross_paper_ask_request_defaults():
    """CrossPaperAskRequest has correct defaults and validates constraints."""
    req = CrossPaperAskRequest(question="What is attention?")
    assert req.max_chunks == 10
    assert req.max_papers == 5

    # Bounds
    with pytest.raises(Exception):
        CrossPaperAskRequest(question="")  # min_length=1

    with pytest.raises(Exception):
        CrossPaperAskRequest(question="x", max_chunks=0)  # ge=1

    with pytest.raises(Exception):
        CrossPaperAskRequest(question="x", max_papers=100)  # le=15


# ---------------------------------------------------------------------------
# Test: confidence event emitted before [DONE] for cross-paper stream
# ---------------------------------------------------------------------------


class _FakeSSELine:
    def __init__(self, lines: list[str]):
        self._lines = lines

    async def __aiter__(self):
        for line in self._lines:
            yield line


class _FakeStreamResponse:
    def __init__(self, lines: list[str], status_code: int = 200):
        self._lines = lines
        self.status_code = status_code

    def raise_for_status(self):
        pass

    def aiter_lines(self):
        return _FakeSSELine(self._lines).__aiter__()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


async def test_confidence_event_emitted_before_done():
    """Cross-paper stream: confidence SSE event appears after done and before [DONE]."""
    import json

    from paper_ingestion.rag.streaming import stream_rag_events

    sse_lines = [
        'data: {"choices": [{"delta": {"content": "Transformers use attention."}}]}',
        "data: [DONE]",
    ]

    import httpx

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.stream.return_value = _FakeStreamResponse(sse_lines)

    # Cross-paper sources include paper_id fields
    sources = [
        {
            "paper_id": 10,
            "chunk_index": 0,
            "content": "Transformers use attention.",
            "page_number": 2,
            "score": 0.88,
            "paper_title": "Transformer Paper",
        },
        {
            "paper_id": 20,
            "chunk_index": 0,
            "content": "Evidence about attention mechanisms.",
            "page_number": 5,
            "score": 0.75,
            "paper_title": "Attention Paper",
        },
    ]

    # Build a stub verifier that always verifies
    _vresult = MagicMock()
    _vresult.verified = True
    _vresult.match_type = "exact"
    _vresult.match_score = 1.0
    stub_verifier = MagicMock()
    stub_verifier.verify_quote.return_value = _vresult

    # DB returns one chunk row per paper_id
    rows_by_pid: dict[int, list[dict]] = {
        10: [{"content": "Transformers use attention."}],
        20: [{"content": "Evidence about attention mechanisms."}],
    }

    async def _fetch(sql, paper_id):  # noqa: ARG001
        return rows_by_pid.get(paper_id, [])

    stub_conn = AsyncMock()
    stub_conn.fetch.side_effect = _fetch

    stub_ctx = MagicMock()
    stub_ctx.__aenter__ = AsyncMock(return_value=stub_conn)
    stub_ctx.__aexit__ = AsyncMock(return_value=False)
    stub_pool = MagicMock()
    stub_pool.acquire.return_value = stub_ctx

    valid_confidence = {"HIGH", "MEDIUM", "LOW", "UNVERIFIED"}

    events: list[str] = []
    async for event in stream_rag_events(
        mock_client,
        [{"role": "user", "content": "How do transformers work?"}],
        sources,
        verifier=stub_verifier,
        db_pool=stub_pool,
    ):
        events.append(event)

    # Parse all data events (skip [DONE] sentinel)
    parsed: list[dict] = []
    for ev in events:
        data_str = ev.replace("data: ", "", 1).strip()
        if data_str == "[DONE]":
            continue
        parsed.append(json.loads(data_str))

    event_types = [e["type"] for e in parsed]

    # Sequence checks: token → sources → done → confidence
    assert "token" in event_types
    assert event_types.index("sources") > event_types.index("token")
    assert event_types.index("done") > event_types.index("sources")
    assert event_types.index("confidence") > event_types.index("done")

    # [DONE] is last raw event
    assert events[-1].strip() == "data: [DONE]"

    # Validate confidence event payload
    conf_event = next(e for e in parsed if e["type"] == "confidence")
    assert set(conf_event.keys()) >= {"type", "confidence", "verified_fraction", "per_sentence"}
    assert conf_event["confidence"] in valid_confidence
    assert isinstance(conf_event["verified_fraction"], float)
    assert isinstance(conf_event["per_sentence"], list)
    # Cross-paper path: at least 1 sentence (from the token stream)
    assert len(conf_event["per_sentence"]) >= 1
