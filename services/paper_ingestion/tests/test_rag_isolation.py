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
