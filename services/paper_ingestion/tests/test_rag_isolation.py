"""User-scope isolation for the Qdrant-backed RAG layer.

Proves the search functions in ``paper_ingestion.ingestion.embedder`` honour
the ``user_id`` kwarg: caller user_id=A sees only A-tagged chunks + canonical
(NULL) chunks; never user-B chunks.

The BM25 leg of ``hybrid_search`` is intentionally OUT of this filter (Decision 6
of the 2026-05-14 RAG/topics/hygiene sweep — see
docs/plans/2026-05-14-functional-sweep-rag-topics-hygiene.md). User-level
paper visibility belongs at the router layer (e.g. papers/feed joins
paper_user_state), not at the embedder. The semantic leg is scoped; the
BM25 leg surfaces cross-corpus discovery for not-yet-claimed papers.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from paper_ingestion.embedder import Embedder


def _hit(paper_id: int, user_id: int | None, score: float = 0.9) -> SimpleNamespace:
    return SimpleNamespace(
        payload={
            "paper_id": paper_id,
            "chunk_index": 0,
            "content": f"chunk owned by {user_id}",
            "page_number": 1,
            "user_id": user_id,
        },
        score=score,
    )


def _make_embedder(hits: list[SimpleNamespace]) -> tuple[Embedder, AsyncMock]:
    """Return (embedder, mock_qdrant) so tests can inspect mock_qdrant.call_args."""
    mock_qdrant = AsyncMock()
    mock_qdrant.query_points.return_value = SimpleNamespace(points=hits)
    embedder = Embedder(AsyncMock(), mock_qdrant)
    embedder.embed_texts = AsyncMock(return_value=[[0.1, 0.2]])
    return embedder, mock_qdrant


async def test_search_chunks_global_user_scope_passes_filter_to_qdrant():
    """search_chunks_global(user_id=1) sends a should=[user_id==1, is_null] filter."""
    from qdrant_client.models import FieldCondition, IsNullCondition, MatchValue

    embedder, mock_qdrant = _make_embedder([])

    await embedder.search_chunks_global("q", user_id=1)

    qf = mock_qdrant.query_points.call_args.kwargs["query_filter"]
    assert qf is not None, "user_id should produce a non-None filter"
    user_clauses = [c for c in qf.should if isinstance(c, FieldCondition)]
    null_clauses = [c for c in qf.should if isinstance(c, IsNullCondition)]
    assert len(user_clauses) == 1
    assert isinstance(user_clauses[0].match, MatchValue)
    assert user_clauses[0].match.value == 1
    assert len(null_clauses) == 1


async def test_search_chunks_global_no_user_id_no_filter():
    """Legacy single-tenant code path: no user_id → no filter."""
    embedder, mock_qdrant = _make_embedder([_hit(1, 2), _hit(2, 5), _hit(3, None)])

    results = await embedder.search_chunks_global("q")

    assert mock_qdrant.query_points.call_args.kwargs["query_filter"] is None
    assert len(results) == 3


async def test_search_similar_excludes_seed_paper_and_scopes_to_user():
    """must_not[paper_id] rides alongside should[user_id OR null]."""
    from qdrant_client.models import FieldCondition, IsNullCondition, MatchValue

    embedder, mock_qdrant = _make_embedder([])

    await embedder.search_similar("q", paper_id_filter=42, user_id=7)

    qf = mock_qdrant.query_points.call_args.kwargs["query_filter"]
    assert qf.must_not[0].key == "paper_id"
    must_not_clause = qf.must_not[0]
    assert isinstance(must_not_clause, FieldCondition)
    assert isinstance(must_not_clause.match, MatchValue)
    assert must_not_clause.match.value == 42
    user_clauses = [c for c in qf.should if isinstance(c, FieldCondition)]
    null_clauses = [c for c in qf.should if isinstance(c, IsNullCondition)]
    assert len(user_clauses) == 1
    assert isinstance(user_clauses[0].match, MatchValue)
    assert user_clauses[0].match.value == 7
    assert len(null_clauses) == 1


async def test_hybrid_search_threads_user_id_to_semantic_leg_only():
    """hybrid_search passes user_id to search_chunks_global; BM25 SQL is unchanged."""
    embedder, _mock_qdrant = _make_embedder([])
    embedder.search_chunks_global = AsyncMock(return_value=[])

    db_pool = MagicMock()
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])
    db_pool.acquire.return_value.__aenter__.return_value = conn

    await embedder.hybrid_search("neural odes", db_pool=db_pool, limit=5, user_id=7)

    assert embedder.search_chunks_global.call_args.kwargs["user_id"] == 7


async def test_hybrid_search_bm25_leg_does_not_filter_by_user_id():
    """BM25 leg's SQL string must not reference user_id (Decision 6, 2026-05-14 sweep).

    Per-user paper visibility is enforced at the router layer (papers/feed
    endpoint joins paper_user_state); the BM25 leg of hybrid_search
    intentionally surfaces cross-corpus matches so that searches against
    not-yet-claimed papers still work. Regression guard: if someone adds
    a WHERE user_id = $N to the BM25 SQL, this test fails.
    """
    embedder, _mock_qdrant = _make_embedder([])
    embedder.search_chunks_global = AsyncMock(return_value=[])

    db_pool = MagicMock()
    conn = AsyncMock()
    captured_sql: list[str] = []

    async def _capture(sql, *args, **kwargs):
        captured_sql.append(sql)
        return []

    conn.fetch = AsyncMock(side_effect=_capture)
    db_pool.acquire.return_value.__aenter__.return_value = conn

    await embedder.hybrid_search("neural odes", db_pool=db_pool, limit=5, user_id=42)

    assert captured_sql, "BM25 SQL should have been captured"
    sql_text = " ".join(captured_sql).lower()
    assert "user_id" not in sql_text, (
        "BM25 SQL must not reference user_id (Decision 6, 2026-05-14 sweep). "
        f"Captured SQL: {captured_sql}"
    )
    assert "$3" not in sql_text, "BM25 SQL should only have $1 and $2 parameters"


async def test_prepare_cross_paper_rag_threads_user_id():
    """prepare_cross_paper_rag forwards user_id into search_chunks_global."""
    from paper_ingestion.models import CrossPaperAskRequest
    from paper_ingestion.rag.streaming import prepare_cross_paper_rag

    embedder, _mock_qdrant = _make_embedder([])
    embedder.search_chunks_global = AsyncMock(return_value=[])

    db_pool = MagicMock()
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])
    db_pool.acquire.return_value.__aenter__.return_value = conn

    body = CrossPaperAskRequest(
        question="how do neural ODEs handle stiffness?",
        max_chunks=5,
        max_papers=3,
        decompose=False,
    )
    http_client = AsyncMock()

    result = await prepare_cross_paper_rag(embedder, db_pool, body, http_client, user_id=99)

    assert embedder.search_chunks_global.call_args.kwargs["user_id"] == 99
    # No chunks returned → expect the no-results short-circuit
    assert getattr(result, "answer", "").startswith("No relevant")


async def test_cross_paper_rag_two_tenant_isolation():
    """Two-tenant isolation: tenant A's query must not see tenant B's private chunks.

    Regression guard for the Filter(should=[user_id==X, is_null]) scoping in
    ``search_chunks_global``.  If ``_user_scope_filter`` is bypassed or its
    ``should`` clauses are removed, Qdrant would return B's chunk to A, and
    this test fails.

    Mechanism: we let Qdrant return the raw chunk list (bypassing the real
    Qdrant client) and then verify that the Qdrant ``query_filter`` passed for
    tenant A's call would exclude tenant B's chunks — i.e. the filter has a
    ``FieldCondition(user_id == A)`` and an ``IsNullCondition``, but no
    unconditional match for B.
    """
    from qdrant_client.models import FieldCondition, IsNullCondition, MatchValue

    tenant_a = 1
    tenant_b = 2

    # Canonical chunk (user_id=None) — should be visible to everyone
    chunk_canonical = _hit(paper_id=20, user_id=None, score=0.80)

    # The real Qdrant client applies the filter server-side; here we verify
    # the filter *passed* to query_points enforces the correct scoping for tenant_a.
    embedder, mock_qdrant = _make_embedder([chunk_canonical])  # Qdrant "returns" only canonical
    await embedder.search_chunks_global("attention mechanisms", user_id=tenant_a)

    qf = mock_qdrant.query_points.call_args.kwargs["query_filter"]

    # Must have a non-null filter (proves scoping is active for tenant_a)
    assert qf is not None, "No filter passed — cross-tenant leak possible"

    user_clauses = [c for c in qf.should if isinstance(c, FieldCondition)]
    null_clauses = [c for c in qf.should if isinstance(c, IsNullCondition)]

    # Exactly one user-match clause, scoped to tenant_a — NOT tenant_b
    assert len(user_clauses) == 1, f"Expected 1 FieldCondition, got {user_clauses}"
    assert isinstance(user_clauses[0].match, MatchValue)
    assert user_clauses[0].match.value == tenant_a, (
        f"Filter scoped to wrong user: {user_clauses[0].match.value!r} != {tenant_a}"
    )
    assert user_clauses[0].match.value != tenant_b, "Filter would admit tenant B's chunks"

    # Canonical (null) clause present so shared corpus remains visible
    assert len(null_clauses) == 1, "Missing IsNullCondition — canonical chunks would be excluded"

    # No clause that would unconditionally admit tenant_b's private chunks
    b_clauses = [
        c
        for c in qf.should
        if isinstance(c, FieldCondition)
        and isinstance(c.match, MatchValue)
        and c.match.value == tenant_b
    ]
    assert len(b_clauses) == 0, (
        "Filter contains a clause that would admit tenant B's private chunks"
    )


# ---------------------------------------------------------------------------
# RAG-DB-1: metadata fetch respects user_id scope (defense-in-depth)
# ---------------------------------------------------------------------------


def _make_db_row(paper_id: int, title: str, user_id: int | None) -> dict:
    """Simulate an asyncpg Record as a plain dict for test purposes."""
    return {
        "id": paper_id,
        "title": title,
        "authors": "Author",
        "url": "http://x",
        "user_id": user_id,
    }


def _make_pool_with_chunks(
    chunks: list[dict],
    db_rows: list[dict],
) -> tuple:
    """Return (embedder, db_pool) mocks for prepare_cross_paper_rag tests."""
    mock_qdrant = AsyncMock()
    from paper_ingestion.embedder import Embedder

    embedder = Embedder(AsyncMock(), mock_qdrant)
    embedder.embed_texts = AsyncMock(return_value=[[0.1, 0.2]])
    embedder.search_chunks_global = AsyncMock(return_value=chunks)
    embedder.rerank_chunks = AsyncMock(side_effect=lambda q, c, top_k: c[:top_k])

    conn = AsyncMock()
    # Capture the SQL and params so we can assert on them
    captured: list[tuple] = []

    async def _fetch(sql, *args, **kwargs):
        captured.append((sql, args))
        return db_rows

    conn.fetch = AsyncMock(side_effect=_fetch)
    db_pool = MagicMock()
    db_pool.acquire.return_value.__aenter__.return_value = conn
    return embedder, db_pool, captured


async def test_metadata_fetch_excludes_other_user_paper():
    """RAG-DB-1 (a): metadata SQL must include user-scope predicate.

    A paper owned by a different user must be filtered out at the DB layer.
    We verify the SQL contains AND (user_id = $2 OR user_id IS NULL) and that
    the $2 parameter is the caller's user_id.
    """
    from paper_ingestion.models import CrossPaperAskRequest
    from paper_ingestion.rag.streaming import prepare_cross_paper_rag

    # Qdrant returns a chunk for paper_id=5 (owned by user 99, correctly scoped by Qdrant)
    chunks = [
        {
            "paper_id": 5,
            "chunk_index": 0,
            "content": "content from paper 5",
            "page_number": 1,
            "score": 0.9,
        }
    ]
    # DB row for the other user — should be excluded by the predicate
    db_rows: list[dict] = []  # empty: simulates predicate filtered it out

    embedder, db_pool, captured = _make_pool_with_chunks(chunks, db_rows)

    body = CrossPaperAskRequest(
        question="test question",
        max_chunks=5,
        max_papers=3,
        decompose=False,
    )

    await prepare_cross_paper_rag(embedder, db_pool, body, AsyncMock(), user_id=99)

    assert captured, "DB fetch was not called"
    sql, args = captured[0]
    sql_lower = sql.lower()
    assert "user_id = $2" in sql_lower, f"Missing user_id = $2 predicate in metadata SQL: {sql!r}"
    assert "user_id is null" in sql_lower, (
        f"Missing user_id IS NULL predicate in metadata SQL (shared corpus): {sql!r}"
    )
    # The second positional arg must be the caller's user_id
    assert args[1] == 99, f"Expected user_id=99 as $2 parameter, got args={args}"


async def test_metadata_fetch_includes_canonical_paper():
    """RAG-DB-1 (b): canonical papers (user_id IS NULL) are included for all users.

    The SQL predicate must allow user_id IS NULL rows so the shared corpus
    remains accessible. We verify the returned paper_meta includes the canonical
    paper when the DB returns it (i.e. the predicate allows it).
    """
    from paper_ingestion.models import CrossPaperAskRequest
    from paper_ingestion.rag.streaming import CrossPaperRagPrep, prepare_cross_paper_rag

    chunks = [
        {
            "paper_id": 20,
            "chunk_index": 0,
            "content": "canonical content",
            "page_number": 3,
            "score": 0.85,
        }
    ]
    # DB returns the canonical paper (user_id IS NULL allowed by predicate)
    db_rows = [{"id": 20, "title": "Canonical Paper", "authors": "System", "url": "http://c"}]

    embedder, db_pool, captured = _make_pool_with_chunks(chunks, db_rows)

    body = CrossPaperAskRequest(
        question="canonical question",
        max_chunks=5,
        max_papers=3,
        decompose=False,
    )

    result = await prepare_cross_paper_rag(embedder, db_pool, body, AsyncMock(), user_id=7)

    assert isinstance(result, CrossPaperRagPrep), f"Expected CrossPaperRagPrep, got {result!r}"
    titles = [s["paper_title"] for s in result.sources]
    assert "Canonical Paper" in titles, (
        f"Canonical paper missing from sources — shared corpus broken: {titles}"
    )
    # SQL must still have both predicates
    sql, args = captured[0]
    sql_lower = sql.lower()
    assert "user_id = $2" in sql_lower
    assert "user_id is null" in sql_lower
    assert args[1] == 7


async def test_metadata_fetch_same_user_paper_unaffected():
    """RAG-DB-1 (c): regression — same-user papers are still returned normally."""
    from paper_ingestion.models import CrossPaperAskRequest
    from paper_ingestion.rag.streaming import CrossPaperRagPrep, prepare_cross_paper_rag

    chunks = [
        {
            "paper_id": 42,
            "chunk_index": 0,
            "content": "user's own paper content",
            "page_number": 2,
            "score": 0.92,
        }
    ]
    db_rows = [{"id": 42, "title": "My Paper", "authors": "Me", "url": "http://me"}]

    embedder, db_pool, captured = _make_pool_with_chunks(chunks, db_rows)

    body = CrossPaperAskRequest(
        question="my own research",
        max_chunks=5,
        max_papers=3,
        decompose=False,
    )

    result = await prepare_cross_paper_rag(embedder, db_pool, body, AsyncMock(), user_id=5)

    assert isinstance(result, CrossPaperRagPrep), f"Expected CrossPaperRagPrep, got {result!r}"
    titles = [s["paper_title"] for s in result.sources]
    assert "My Paper" in titles, f"Same-user paper missing from sources: {titles}"
    sql, args = captured[0]
    assert args[1] == 5, f"Expected user_id=5 as $2, got {args}"
