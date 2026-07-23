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
# Vector filter construction: current generation AND persisted public scope or
# explicit caller-library membership. Source labels, discoverer audit values,
# and legacy vector owners have no authorization role.
# ---------------------------------------------------------------------------


def test_user_scope_filter_is_generation_and_persisted_scope_authority():
    """Private access widens only to the caller's supplied library paper IDs."""
    from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchValue

    from paper_ingestion.ingestion.embedding_config import _user_scope_filter

    caller_b = 2
    library_paper = 100
    other_private_paper = 200
    generation = "1" * 32

    flt = _user_scope_filter(
        caller_b,
        library_paper_ids=[library_paper],
        visibility_generation=generation,
    )
    assert flt is not None
    assert len(flt.must) == 2
    generation_condition = flt.must[0]
    assert isinstance(generation_condition, FieldCondition)
    assert generation_condition.key == "visibility_generation"
    assert isinstance(generation_condition.match, MatchValue)
    assert generation_condition.match.value == generation

    access = flt.must[1]
    assert isinstance(access, Filter)
    assert len(access.should) == 2
    public = access.should[0]
    assert isinstance(public, FieldCondition)
    assert public.key == "visibility_scope"
    assert public.match.value == "public"

    private_library = access.should[1]
    assert isinstance(private_library, Filter)
    private_conditions = {
        condition.key: condition for condition in private_library.must if isinstance(condition, FieldCondition)
    }
    assert private_conditions["visibility_scope"].match.value == "private"
    assert isinstance(private_conditions["paper_id"].match, MatchAny)
    assert set(private_conditions["paper_id"].match.any) == {library_paper}
    assert other_private_paper not in private_conditions["paper_id"].match.any

    all_keys = {
        condition.key
        for condition in [generation_condition, public, *private_library.must]
        if isinstance(condition, FieldCondition)
    }
    assert "source_type" not in all_keys
    assert "user_id" not in all_keys


def test_user_scope_filter_with_empty_library_keeps_only_public_scope():
    """An authenticated caller without memberships receives public vectors only."""
    from qdrant_client.models import FieldCondition, Filter

    from paper_ingestion.ingestion.embedding_config import _user_scope_filter

    flt = _user_scope_filter(5, library_paper_ids=[], visibility_generation="2" * 32)
    assert flt is not None
    access = flt.must[1]
    assert isinstance(access, Filter)
    assert len(access.should) == 1
    assert isinstance(access.should[0], FieldCondition)
    assert access.should[0].key == "visibility_scope"
    assert access.should[0].match.value == "public"


def test_missing_generation_fails_closed_while_internal_scope_is_explicit():
    """Missing checkpoint metadata cannot fall back to an unscoped query."""
    from qdrant_client.models import FieldCondition, MatchValue

    from paper_ingestion.ingestion.embedding_config import _user_scope_filter

    assert _user_scope_filter(None) is None
    flt = _user_scope_filter(22, library_paper_ids=[], visibility_generation=None)
    assert flt is not None
    generation = flt.must[0]
    assert isinstance(generation, FieldCondition)
    assert generation.key == "visibility_generation"
    assert isinstance(generation.match, MatchValue)
    assert generation.match.value == "0" * 32


# ---------------------------------------------------------------------------
# Persisted visibility: the DB-side backstop, exercised against real
# Postgres so the predicate itself is evaluated (a mock pool would return rows
# regardless of the SQL and prove nothing).
#
# A persisted-public paper remains visible regardless of library membership.
# Private papers require an explicit row in the requesting user's library;
# discovery attribution is not authorization.
# ---------------------------------------------------------------------------


async def _seed_paper(
    conn,
    external_id: str,
    *,
    visibility_scope: str,
    discovered_by: int | None = None,
) -> int:
    return await conn.fetchval(
        """INSERT INTO papers (
               external_id, source_type, title, authors, url,
               discovered_by, visibility_scope
           )
           VALUES ($1, 'arxiv', 'Shared Corpus Paper', ARRAY['Author'],
                   'https://shared.test/paper', $2, $3)
           RETURNING id""",
        external_id,
        discovered_by,
        visibility_scope,
    )


async def _shelve(conn, user_id: int, paper_id: int) -> None:
    await conn.execute(
        "INSERT INTO user_library (user_id, paper_id, added_via) VALUES ($1, $2, 'manual_save')",
        user_id,
        paper_id,
    )


def _embedder_returning(chunks: list[dict]):
    mock_embedder = MagicMock()
    mock_embedder.search_chunks_global = AsyncMock(return_value=chunks)
    mock_embedder.rerank_chunks = AsyncMock(side_effect=lambda q, c, top_k: c[:top_k])
    return mock_embedder


def _chunk(paper_id: int, score: float) -> dict:
    return {
        "paper_id": paper_id,
        "chunk_index": 0,
        "content": "Attention mechanisms weight token pairs.",
        "page_number": 1,
        "score": score,
    }


@pytest.mark.contract
@pytest.mark.real_auth
@pytest.mark.asyncio(loop_scope="session")
async def test_public_paper_stays_visible_after_another_user_shelves_it(
    contract_two_users,
    contract_conn,
):
    """A persisted-public paper shelved only by A still reaches B's answers.

    B never shelved it, so the caller's-library branch cannot admit it; only
    the public-scope branch can. If that branch is missing, the metadata
    fetch drops the row, the chunk is filtered out, and B degrades to
    no-results — silently losing a paper the whole install is meant to share.
    """
    from jarvis_common.testing import SharedConnPool
    from paper_ingestion.rag.streaming import CrossPaperRagPrep, prepare_cross_paper_rag

    public_id = await _seed_paper(
        contract_conn,
        "shared-public",
        visibility_scope="public",
    )
    await _shelve(contract_conn, contract_two_users.user_a_id, public_id)

    result = await prepare_cross_paper_rag(
        _embedder_returning([_chunk(public_id, 0.9)]),
        SharedConnPool(contract_conn),
        CrossPaperAskRequest(question="How does attention work?", decompose=False),
        AsyncMock(),
        user_id=contract_two_users.user_b_id,
    )

    assert isinstance(result, CrossPaperRagPrep), (
        f"B must still receive the persisted-public paper; got {result!r}"
    )
    assert {s["paper_id"] for s in result.sources} == {public_id}


@pytest.mark.contract
@pytest.mark.real_auth
@pytest.mark.asyncio(loop_scope="session")
async def test_private_papers_outside_library_stay_out_of_cross_paper_answers(
    contract_two_users,
    contract_conn,
):
    """Private papers outside B's library never reach B's answer.

    The persisted-public control proves the pipeline reached the
    metadata fetch — without it, an empty source list would also be produced
    by any earlier short-circuit.
    """
    from jarvis_common.testing import SharedConnPool
    from paper_ingestion.rag.streaming import CrossPaperRagPrep, prepare_cross_paper_rag

    user_a = contract_two_users.user_a_id
    shelved_by_a = await _seed_paper(
        contract_conn,
        "private-shelved",
        visibility_scope="private",
        discovered_by=user_a,
    )
    await _shelve(contract_conn, user_a, shelved_by_a)
    unshelved = await _seed_paper(
        contract_conn,
        "private-unshelved",
        visibility_scope="private",
        discovered_by=user_a,
    )
    control = await _seed_paper(
        contract_conn,
        "public-control",
        visibility_scope="public",
    )

    result = await prepare_cross_paper_rag(
        _embedder_returning(
            [_chunk(control, 0.90), _chunk(shelved_by_a, 0.89), _chunk(unshelved, 0.88)]
        ),
        SharedConnPool(contract_conn),
        CrossPaperAskRequest(question="How does attention work?", decompose=False),
        AsyncMock(),
        user_id=contract_two_users.user_b_id,
    )

    assert isinstance(result, CrossPaperRagPrep), f"the control paper must survive; got {result!r}"
    assert {s["paper_id"] for s in result.sources} == {control}, (
        "only the persisted-public paper is visible to B"
    )


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
