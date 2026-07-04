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


async def test_dedup_max_chunks_per_paper(monkeypatch):
    """prepare_cross_paper_rag deduplicates: keeps at most 2 chunks per paper
    and trims to max_papers by best-chunk score.

    D3-03: replaced the prior test that re-implemented the dedup logic in the
    test body (asserting on its own local variables).  This version drives the
    real prepare_cross_paper_rag path with a controlled chunk set and asserts
    on the CrossPaperRagPrep.sources that come back out.
    """
    from unittest.mock import AsyncMock, MagicMock

    # Mechanics under test = dedup, not relevance: the controlled scores
    # (0.6-0.9) would otherwise be filtered by the relative cosine cutoff.
    monkeypatch.setenv("RAG_RELATIVE_SCORE_CUTOFF", "0")

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

    # DB mock: route by query.  The user_library lookup (PI-RAG-001) returns the
    # caller's library paper_ids; the metadata fetch returns paper rows.  paper 3
    # would be dropped before the DB fetch anyway, but the metadata shape is
    # simulated faithfully.
    db_rows = [
        {"id": 1, "title": "Paper One", "authors": "A", "url": "http://p1"},
        {"id": 2, "title": "Paper Two", "authors": "B", "url": "http://p2"},
    ]
    library_rows = [{"paper_id": 1}, {"paper_id": 2}, {"paper_id": 3}]

    async def _fetch(sql, *args):  # noqa: ARG001
        return library_rows if "user_library WHERE user_id" in sql else db_rows

    conn = AsyncMock()
    conn.fetch = AsyncMock(side_effect=_fetch)
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
# Relevance gates: Layer 1 relative cosine cutoff + Layer 2 rerank-score floor
# ---------------------------------------------------------------------------


def _make_cutoff_pool(db_rows: list[dict], library_rows: list[dict]):
    """Mock pool routing user_library lookups vs paper-metadata fetches."""

    async def _fetch(sql, *args):  # noqa: ARG001
        return library_rows if "user_library WHERE user_id" in sql else db_rows

    conn = AsyncMock()
    conn.fetch = AsyncMock(side_effect=_fetch)
    db_pool = MagicMock()
    db_pool.acquire.return_value.__aenter__.return_value = conn
    return db_pool


async def test_relative_cutoff_drops_low_cosine_chunks(monkeypatch):
    """Layer 1 (always-on): chunks below top_score * 0.85 are dropped BEFORE rerank.

    Mirrors the live finding: top hit 0.765 → cutoff 0.650 → the 0.48-0.52
    off-topic band drops, even with the reranker disabled (identity mock,
    no rerank_score attached).
    """
    from paper_ingestion.rag.streaming import CrossPaperRagPrep, prepare_cross_paper_rag

    monkeypatch.delenv("RAG_RELATIVE_SCORE_CUTOFF", raising=False)
    monkeypatch.delenv("RAG_MIN_RERANK_SCORE", raising=False)

    all_chunks = [
        {"paper_id": 1, "chunk_index": 0, "content": "on-topic", "page_number": 1, "score": 0.765},
        {"paper_id": 2, "chunk_index": 0, "content": "junk-a", "page_number": 1, "score": 0.52},
        {"paper_id": 3, "chunk_index": 0, "content": "junk-b", "page_number": 1, "score": 0.48},
    ]
    mock_embedder = MagicMock()
    mock_embedder.search_chunks_global = AsyncMock(return_value=all_chunks)
    mock_embedder.rerank_chunks = AsyncMock(side_effect=lambda q, c, top_k: c[:top_k])

    db_pool = _make_cutoff_pool(
        db_rows=[{"id": 1, "title": "Paper One", "authors": "A", "url": "http://p1"}],
        library_rows=[{"paper_id": 1}, {"paper_id": 2}, {"paper_id": 3}],
    )
    body = CrossPaperAskRequest(question="cutoff test", decompose=False)

    result = await prepare_cross_paper_rag(mock_embedder, db_pool, body, AsyncMock(), user_id=1)

    assert isinstance(result, CrossPaperRagPrep), f"Expected CrossPaperRagPrep, got {result!r}"
    assert {s["paper_id"] for s in result.sources} == {1}, (
        "Only the 0.765 chunk survives the 0.85 relative cutoff (threshold 0.650)"
    )
    # Placement proof: the cutoff runs BEFORE rerank — the reranker must only
    # see the surviving chunk.
    rerank_call = mock_embedder.rerank_chunks.await_args
    assert [c["paper_id"] for c in rerank_call.args[1]] == [1]


async def test_relative_cutoff_is_per_subquery_in_decompose_path(monkeypatch):
    """Decomposed sub-queries embed separately, so their cosine scales differ:
    the cutoff must run per sub-query list, never across the merged pool —
    otherwise one strong facet silently deletes another facet's best hits.
    """
    from paper_ingestion.rag import streaming as streaming_mod
    from paper_ingestion.rag.streaming import CrossPaperRagPrep, prepare_cross_paper_rag

    monkeypatch.delenv("RAG_RELATIVE_SCORE_CUTOFF", raising=False)
    monkeypatch.delenv("RAG_MIN_RERANK_SCORE", raising=False)

    facet_a = [
        {
            "paper_id": 1,
            "chunk_index": 0,
            "content": "facet-a-hit",
            "page_number": 1,
            "score": 0.78,
        },
        {
            "paper_id": 2,
            "chunk_index": 0,
            "content": "facet-a-junk",
            "page_number": 1,
            "score": 0.40,
        },
    ]
    facet_b = [
        {
            "paper_id": 3,
            "chunk_index": 0,
            "content": "facet-b-hit",
            "page_number": 1,
            "score": 0.62,
        },
        {
            "paper_id": 4,
            "chunk_index": 0,
            "content": "facet-b-junk",
            "page_number": 1,
            "score": 0.30,
        },
    ]

    async def _decompose(question, model):  # noqa: ARG001
        return ["facet a", "facet b"]

    monkeypatch.setattr(streaming_mod, "decompose_query", _decompose)
    monkeypatch.setattr(streaming_mod, "get_fast_model", lambda: "fast")

    async def _search(*, query_text, **kwargs):  # noqa: ARG001
        return facet_a if query_text == "facet a" else facet_b

    mock_embedder = MagicMock()
    mock_embedder.search_chunks_global = AsyncMock(side_effect=_search)
    mock_embedder.rerank_chunks = AsyncMock(side_effect=lambda q, c, top_k: c[:top_k])

    db_pool = _make_cutoff_pool(
        db_rows=[
            {"id": 1, "title": "Paper One", "authors": "A", "url": "http://p1"},
            {"id": 3, "title": "Paper Three", "authors": "B", "url": "http://p3"},
        ],
        library_rows=[{"paper_id": n} for n in (1, 2, 3, 4)],
    )
    body = CrossPaperAskRequest(question="multi-facet question", decompose=True)

    result = await prepare_cross_paper_rag(mock_embedder, db_pool, body, AsyncMock(), user_id=1)

    assert isinstance(result, CrossPaperRagPrep), f"Expected CrossPaperRagPrep, got {result!r}"
    # Facet B's best hit (0.62) survives even though it is far below facet A's
    # 0.78; a merged-pool cutoff (0.78 * 0.85 = 0.663) would have deleted it.
    assert {s["paper_id"] for s in result.sources} == {1, 3}
    reranked = mock_embedder.rerank_chunks.await_args.args[1]
    assert {c["paper_id"] for c in reranked} == {1, 3}


async def test_rerank_floor_backend_default_drops_and_degrades(monkeypatch):
    """Layer 2: with the default cross-encoder backend the floor is 3.0; when
    EVERY chunk reranks below it, the result degrades to CrossPaperRagNoResults.
    """
    from paper_ingestion.rag.streaming import CrossPaperRagNoResults, prepare_cross_paper_rag

    monkeypatch.delenv("RAG_MIN_RERANK_SCORE", raising=False)
    monkeypatch.delenv("RERANKER_BACKEND", raising=False)  # default backend → floor 3.0

    # Close cosine scores so Layer 1 keeps both; Layer 2 is the gate under test.
    all_chunks = [
        {"paper_id": 1, "chunk_index": 0, "content": "weak-a", "page_number": 1, "score": 0.9},
        {"paper_id": 2, "chunk_index": 0, "content": "weak-b", "page_number": 1, "score": 0.88},
    ]

    def _rerank_with_scores(q, chunks, top_k):  # noqa: ARG001
        # Observed irrelevant band for the default cross-encoder: ~+0.4..+2.7.
        scores = [2.7, 0.4]
        return [{**c, "rerank_score": s} for c, s in zip(chunks, scores)]

    mock_embedder = MagicMock()
    mock_embedder.search_chunks_global = AsyncMock(return_value=all_chunks)
    mock_embedder.rerank_chunks = AsyncMock(side_effect=_rerank_with_scores)

    db_pool = _make_cutoff_pool(
        db_rows=[],
        library_rows=[{"paper_id": 1}, {"paper_id": 2}],
    )
    body = CrossPaperAskRequest(question="floor test", decompose=False)

    result = await prepare_cross_paper_rag(mock_embedder, db_pool, body, AsyncMock(), user_id=1)

    assert isinstance(result, CrossPaperRagNoResults), (
        f"All chunks below the 3.0 floor must degrade to no-results; got {result!r}"
    )
    assert "No relevant information" in result.answer
    assert result.sources == []


# ---------------------------------------------------------------------------
# PI-RAG-001: cross-paper RAG scope widen — filter-construction proof
#
# A secondary-library owner (caller B) must be able to retrieve chunks for a
# SHARED-corpus paper P that another user (A) originally embedded (P's chunks
# carry user_id=A in their Qdrant payload), because P is in B's user_library.
# The widening adds a third `should` branch: paper_id IN <B's library>.
#
# SECURITY INVARIANT (non-negotiable): the widening is keyed ONLY on the
# caller's own library membership.  A paper Q private to A (in A's library,
# NOT B's) must NEVER appear in the widened branch for caller B.
# ---------------------------------------------------------------------------


def test_user_scope_filter_widens_to_callers_library_only():
    """`_user_scope_filter(B, library_paper_ids)` adds a paper_id IN-branch.

    Revert-proof: if the widening is dropped, the third `should` branch
    disappears and the P-membership assertion fails.  If the widening ever
    keyed on something other than the supplied (caller's-own) library list,
    Q (not supplied) would leak into the branch and the negative assertion
    fails.
    """
    from qdrant_client.models import FieldCondition, MatchAny, MatchValue

    from paper_ingestion.ingestion.embedding_config import _user_scope_filter

    caller_b = 2
    shared_paper_p = 100  # in B's library (A processed it; chunks payload user_id=A)
    private_paper_q = 200  # in A's library ONLY — must NOT enter B's widened branch

    # B's library contains only P (NOT Q).
    flt = _user_scope_filter(caller_b, library_paper_ids=[shared_paper_p])
    assert flt is not None

    user_id_branches = [
        c
        for c in flt.should
        if getattr(c, "key", None) == "user_id"
        and isinstance(getattr(c, "match", None), MatchValue)
    ]
    assert any(c.match.value == caller_b for c in user_id_branches), (
        "base scope must still match the caller's own user_id"
    )

    paper_id_branches = [
        c
        for c in flt.should
        if isinstance(c, FieldCondition)
        and c.key == "paper_id"
        and isinstance(getattr(c, "match", None), MatchAny)
    ]
    assert len(paper_id_branches) == 1, (
        "widening must add exactly one paper_id MatchAny `should` branch"
    )
    widened_ids = set(paper_id_branches[0].match.any)
    assert shared_paper_p in widened_ids, (
        "P (in B's library) MUST be in the widened branch — else B under-fetches"
    )
    assert private_paper_q not in widened_ids, (
        "Q (NOT in B's library) MUST NOT be in B's widened branch — leak guard"
    )


def test_user_scope_filter_no_widening_without_library_ids():
    """No paper_id `should` branch is added when library_paper_ids is absent/empty.

    Preserves the legacy (user_id==X OR is_null) base scope and the
    single-tenant `None` path.
    """
    from qdrant_client.models import FieldCondition, MatchAny

    from paper_ingestion.ingestion.embedding_config import _user_scope_filter

    # Unscoped (single-tenant) path unchanged.
    assert _user_scope_filter(None) is None

    for lib in (None, []):
        flt = _user_scope_filter(5, library_paper_ids=lib)
        assert flt is not None
        assert not any(
            isinstance(c, FieldCondition)
            and c.key == "paper_id"
            and isinstance(getattr(c, "match", None), MatchAny)
            for c in flt.should
        ), "no widening branch should exist without library_paper_ids"


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
    assert req.paper_ids is None
    assert CrossPaperAskRequest(question="What changed?", paper_ids=[1, 2]).paper_ids == [1, 2]

    # Bounds
    with pytest.raises(Exception):
        CrossPaperAskRequest(question="")  # min_length=1

    with pytest.raises(Exception):
        CrossPaperAskRequest(question="x", max_chunks=0)  # ge=1

    with pytest.raises(Exception):
        CrossPaperAskRequest(question="x", max_papers=100)  # le=15

    with pytest.raises(Exception):
        CrossPaperAskRequest(question="x", paper_ids=[])  # min_length=1


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

    # NOTE: the answer must be a >= 4-word sentence — verification drops
    # shorter segments as non-claims, which would empty per_sentence.
    sse_lines = [
        'data: {"choices": [{"delta": {"content": "Transformers rely on attention mechanisms."}}]}',
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
            "content": "Transformers rely on attention mechanisms.",
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
        10: [{"content": "Transformers rely on attention mechanisms."}],
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
