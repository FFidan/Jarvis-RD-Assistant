"""User-scope isolation for the Qdrant-backed RAG layer.

Proves the search functions in ``paper_ingestion.ingestion.embedder`` honour
the ``user_id`` kwarg: caller user_id=A sees only A-tagged chunks + canonical
(NULL) chunks; never user-B chunks.

The BM25 leg of ``hybrid_search`` is out of scope for this filter (Decision 6
in the 2026-05-14 plan): user-level paper visibility belongs at the router
layer, not at the embedder. We assert the semantic leg receives the scope.
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


def _make_embedder(hits: list[SimpleNamespace]) -> Embedder:
    mock_qdrant = AsyncMock()
    mock_qdrant.query_points.return_value = SimpleNamespace(points=hits)
    embedder = Embedder(AsyncMock(), mock_qdrant)
    embedder.embed_texts = AsyncMock(return_value=[[0.1, 0.2]])
    return embedder


async def test_search_chunks_global_user_scope_passes_filter_to_qdrant():
    """search_chunks_global(user_id=1) sends a should=[user_id==1, is_null] filter."""
    from qdrant_client.models import FieldCondition, IsNullCondition

    embedder = _make_embedder([])

    await embedder.search_chunks_global("q", user_id=1)

    qf = embedder.qdrant.query_points.call_args.kwargs["query_filter"]
    assert qf is not None, "user_id should produce a non-None filter"
    user_clauses = [c for c in qf.should if isinstance(c, FieldCondition)]
    null_clauses = [c for c in qf.should if isinstance(c, IsNullCondition)]
    assert len(user_clauses) == 1 and user_clauses[0].match.value == 1
    assert len(null_clauses) == 1


async def test_search_chunks_global_no_user_id_no_filter():
    """Legacy single-tenant code path: no user_id → no filter."""
    embedder = _make_embedder([_hit(1, 2), _hit(2, 5), _hit(3, None)])

    results = await embedder.search_chunks_global("q")

    assert embedder.qdrant.query_points.call_args.kwargs["query_filter"] is None
    assert len(results) == 3


async def test_search_similar_excludes_seed_paper_and_scopes_to_user():
    """must_not[paper_id] rides alongside should[user_id OR null]."""
    from qdrant_client.models import FieldCondition, IsNullCondition

    embedder = _make_embedder([])

    await embedder.search_similar("q", paper_id_filter=42, user_id=7)

    qf = embedder.qdrant.query_points.call_args.kwargs["query_filter"]
    assert qf.must_not[0].key == "paper_id"
    assert qf.must_not[0].match.value == 42
    user_clauses = [c for c in qf.should if isinstance(c, FieldCondition)]
    null_clauses = [c for c in qf.should if isinstance(c, IsNullCondition)]
    assert user_clauses[0].match.value == 7
    assert len(null_clauses) == 1


async def test_hybrid_search_threads_user_id_to_semantic_leg_only():
    """hybrid_search passes user_id to search_chunks_global; BM25 SQL is unchanged."""
    embedder = _make_embedder([])
    embedder.search_chunks_global = AsyncMock(return_value=[])

    db_pool = MagicMock()
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])
    db_pool.acquire.return_value.__aenter__.return_value = conn

    await embedder.hybrid_search("neural odes", db_pool=db_pool, limit=5, user_id=7)

    assert embedder.search_chunks_global.call_args.kwargs["user_id"] == 7


async def test_prepare_cross_paper_rag_threads_user_id():
    """prepare_cross_paper_rag forwards user_id into search_chunks_global."""
    from paper_ingestion.rag.streaming import prepare_cross_paper_rag

    embedder = _make_embedder([])
    embedder.search_chunks_global = AsyncMock(return_value=[])

    db_pool = MagicMock()
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])
    db_pool.acquire.return_value.__aenter__.return_value = conn

    body = SimpleNamespace(
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
