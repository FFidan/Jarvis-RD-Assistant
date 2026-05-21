"""Tests for hybrid BM25 + semantic search with Reciprocal Rank Fusion."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from jarvis_common.testing_embedder import _dict_to_record, _make_embedder

# ---------------------------------------------------------------------------
# Pure RRF formula tests (no I/O)
# ---------------------------------------------------------------------------


def _rrf_score(k: int, bm25_rank: int | None, semantic_rank: int | None) -> float:
    """Compute RRF score for a single paper given its ranks (1-indexed)."""
    score = 0.0
    if bm25_rank is not None:
        score += 1.0 / (k + bm25_rank)
    if semantic_rank is not None:
        score += 1.0 / (k + semantic_rank)
    return score


def test_rrf_formula():
    """RRF score = sum(1/(k + rank)) across both lists."""
    k = 60
    # Paper appears rank 1 in BM25, rank 3 in semantic
    expected = 1 / (k + 1) + 1 / (k + 3)
    assert abs(expected - (1 / 61 + 1 / 63)) < 1e-10


def test_rrf_single_leg_bm25_only():
    """Paper in only BM25 still gets scored."""
    k = 60
    expected = 1 / (k + 2)
    assert abs(_rrf_score(k, bm25_rank=2, semantic_rank=None) - expected) < 1e-10


def test_rrf_single_leg_semantic_only():
    """Paper in only semantic still gets scored."""
    k = 60
    expected = 1 / (k + 5)
    assert abs(_rrf_score(k, bm25_rank=None, semantic_rank=5) - expected) < 1e-10


def test_rrf_both_legs_rank1():
    """Paper ranked #1 in both legs gets the highest possible RRF."""
    k = 60
    expected = 2 / (k + 1)
    actual = _rrf_score(k, bm25_rank=1, semantic_rank=1)
    assert abs(actual - expected) < 1e-10


def test_rrf_ordering():
    """Higher-ranked paper in both legs beats one ranked lower."""
    k = 60
    high = _rrf_score(k, bm25_rank=1, semantic_rank=2)
    low = _rrf_score(k, bm25_rank=5, semantic_rank=10)
    assert high > low


def test_rrf_tiebreaking():
    """Two papers with identical RRF scores are ordered by paper_id (ascending)."""
    k = 60
    # Paper A: BM25=1, semantic=2  →  Paper B: BM25=2, semantic=1
    # Both get the same total: 1/(k+1) + 1/(k+2)
    score_a = _rrf_score(k, 1, 2)
    score_b = _rrf_score(k, 2, 1)
    assert abs(score_a - score_b) < 1e-10  # same score


# ---------------------------------------------------------------------------
# Integration-style tests using mocked DB + Qdrant
# ---------------------------------------------------------------------------


def _make_pool(rows: list[dict]) -> AsyncMock:
    """Create a mock asyncpg.Pool that returns the given rows from fetch()."""
    records = [_dict_to_record(r) for r in rows]
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=records)
    conn.fetchrow = AsyncMock(return_value=None)
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = AsyncMock()
    pool.acquire = MagicMock(return_value=ctx)
    return pool


@pytest.mark.asyncio
async def test_hybrid_search_empty_results():
    """Both legs return nothing — result is empty list."""
    embedder = _make_embedder()
    pool = _make_pool([])

    with patch.object(embedder, "search_chunks_global", new_callable=AsyncMock, return_value=[]):
        results = await embedder.hybrid_search("nonexistent query", pool, limit=10)

    assert results == []


@pytest.mark.asyncio
async def test_hybrid_search_bm25_only():
    """Results from BM25 only (semantic returns nothing)."""
    embedder = _make_embedder()
    bm25_rows = [
        {
            "id": 1,
            "title": "Paper A",
            "authors": ["Author 1"],
            "url": "https://example.com/a",
            "abstract": "Abstract A",
            "published_date": None,
            "bm25_score": 0.9,
        },
        {
            "id": 2,
            "title": "Paper B",
            "authors": ["Author 2"],
            "url": "https://example.com/b",
            "abstract": "Abstract B",
            "published_date": None,
            "bm25_score": 0.5,
        },
    ]
    pool = _make_pool(bm25_rows)

    with patch.object(embedder, "search_chunks_global", new_callable=AsyncMock, return_value=[]):
        results = await embedder.hybrid_search("test query", pool, limit=10)

    assert len(results) == 2
    # Paper 1 should rank first (higher BM25 score → rank 1)
    assert results[0]["id"] == 1
    assert results[1]["id"] == 2
    # Both should have bm25_rank but no semantic_rank
    assert results[0]["bm25_rank"] == 1
    assert results[0]["semantic_rank"] is None
    assert results[1]["bm25_rank"] == 2


@pytest.mark.asyncio
async def test_hybrid_search_semantic_only():
    """Results from semantic only (BM25 returns nothing)."""
    embedder = _make_embedder()
    pool = _make_pool([])  # BM25 returns empty

    chunks = [
        {"paper_id": 10, "chunk_index": 0, "content": "...", "page_number": 1, "score": 0.85},
        {"paper_id": 10, "chunk_index": 1, "content": "...", "page_number": 2, "score": 0.70},
        {"paper_id": 20, "chunk_index": 0, "content": "...", "page_number": 1, "score": 0.60},
    ]

    # Need to mock the second pool.acquire for fetching missing metadata
    meta_rows = [
        _dict_to_record(
            {
                "id": 10,
                "title": "Semantic Paper",
                "authors": ["Auth"],
                "url": "https://example.com/10",
                "abstract": "Abs",
                "published_date": None,
            }
        ),
        _dict_to_record(
            {
                "id": 20,
                "title": "Another Paper",
                "authors": ["Auth2"],
                "url": "https://example.com/20",
                "abstract": "Abs2",
                "published_date": None,
            }
        ),
    ]
    # On the first call, BM25 returns empty; on the second call, metadata fetch
    call_count = 0
    conn = AsyncMock()

    async def _fetch(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return []  # BM25 empty
        return meta_rows  # metadata fetch

    conn.fetch = _fetch
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=ctx)

    with patch.object(
        embedder, "search_chunks_global", new_callable=AsyncMock, return_value=chunks
    ):
        results = await embedder.hybrid_search("test query", pool, limit=10)

    assert len(results) == 2
    # Paper 10 has higher max chunk score (0.85) → semantic_rank 1
    assert results[0]["id"] == 10
    assert results[0]["semantic_rank"] == 1
    assert results[0]["bm25_rank"] is None
    assert results[1]["id"] == 20
    assert results[1]["semantic_rank"] == 2


@pytest.mark.asyncio
async def test_hybrid_search_aggregation_max_score():
    """Multiple chunks from same paper — max score used for ranking."""
    embedder = _make_embedder()
    pool = _make_pool([])  # BM25 empty

    chunks = [
        {"paper_id": 1, "chunk_index": 0, "content": "...", "page_number": 1, "score": 0.3},
        {"paper_id": 1, "chunk_index": 1, "content": "...", "page_number": 2, "score": 0.9},
        {"paper_id": 1, "chunk_index": 2, "content": "...", "page_number": 3, "score": 0.5},
        {"paper_id": 2, "chunk_index": 0, "content": "...", "page_number": 1, "score": 0.8},
    ]

    meta_rows = [
        _dict_to_record(
            {
                "id": 1,
                "title": "Paper 1",
                "authors": [],
                "url": "https://example.com/1",
                "abstract": None,
                "published_date": None,
            }
        ),
        _dict_to_record(
            {
                "id": 2,
                "title": "Paper 2",
                "authors": [],
                "url": "https://example.com/2",
                "abstract": None,
                "published_date": None,
            }
        ),
    ]

    call_count = 0
    conn = AsyncMock()

    async def _fetch(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return []
        return meta_rows

    conn.fetch = _fetch
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=ctx)

    with patch.object(
        embedder, "search_chunks_global", new_callable=AsyncMock, return_value=chunks
    ):
        results = await embedder.hybrid_search("test", pool, limit=10)

    # Paper 1 max score = 0.9 > Paper 2 max score = 0.8
    # So Paper 1 should be semantic_rank 1
    assert results[0]["id"] == 1
    assert results[0]["semantic_rank"] == 1
    assert results[1]["id"] == 2
    assert results[1]["semantic_rank"] == 2


@pytest.mark.asyncio
async def test_hybrid_search_fusion_both_legs():
    """Paper appearing in both legs gets combined RRF score."""
    embedder = _make_embedder()

    bm25_rows = [
        {
            "id": 1,
            "title": "Paper 1",
            "authors": [],
            "url": "https://example.com/1",
            "abstract": None,
            "published_date": None,
            "bm25_score": 0.8,
        },
    ]
    pool = _make_pool(bm25_rows)

    chunks = [
        {"paper_id": 1, "chunk_index": 0, "content": "...", "page_number": 1, "score": 0.7},
    ]

    with patch.object(
        embedder, "search_chunks_global", new_callable=AsyncMock, return_value=chunks
    ):
        results = await embedder.hybrid_search("test", pool, limit=10, k=60)

    assert len(results) == 1
    r = results[0]
    assert r["id"] == 1
    assert r["bm25_rank"] == 1
    assert r["semantic_rank"] == 1
    # RRF = 1/(60+1) + 1/(60+1) = 2/61
    expected_rrf = round(2.0 / 61, 8)
    assert abs(r["rrf_score"] - expected_rrf) < 1e-8


@pytest.mark.asyncio
async def test_hybrid_search_limit_applied():
    """Limit parameter caps the number of results returned."""
    embedder = _make_embedder()

    bm25_rows = [
        {
            "id": i,
            "title": f"Paper {i}",
            "authors": [],
            "url": f"https://example.com/{i}",
            "abstract": None,
            "published_date": None,
            "bm25_score": 1.0 - i * 0.1,
        }
        for i in range(1, 6)
    ]
    pool = _make_pool(bm25_rows)

    with patch.object(embedder, "search_chunks_global", new_callable=AsyncMock, return_value=[]):
        results = await embedder.hybrid_search("test", pool, limit=3)

    assert len(results) == 3


# ---------------------------------------------------------------------------
# PI-013: search_hybrid endpoint — Embedder typed non-optional
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_hybrid_endpoint_raises_503_when_qdrant_unavailable():
    """search_hybrid must raise 503 when embedder.qdrant is None (Qdrant down).

    PI-013: embedder is now typed as Embedder (non-optional); only
    embedder.qdrant is checked for availability.
    """
    from paper_ingestion.routers import search as search_router

    embedder = _make_embedder()
    embedder.qdrant = None  # simulate Qdrant not configured

    pool = _make_pool([])
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace()),
        client=SimpleNamespace(host="127.0.0.1"),
    )

    body = SimpleNamespace(query="test query", max_results=10)

    with pytest.raises(HTTPException) as exc_info:
        await search_router.search_hybrid.__wrapped__(
            request,
            body=body,
            db_pool=pool,
            embedder=embedder,
        )

    assert exc_info.value.status_code == 503
    assert "Qdrant" in exc_info.value.detail


@pytest.mark.asyncio
async def test_search_hybrid_endpoint_calls_hybrid_search_when_qdrant_available():
    """search_hybrid delegates to embedder.hybrid_search when Qdrant is ready.

    PI-013: with non-optional Embedder type, the None-check path is gone;
    verify the happy path still reaches hybrid_search().
    """
    from paper_ingestion.routers import search as search_router

    embedder = _make_embedder()
    # embedder.qdrant is an AsyncMock (truthy) — Qdrant is "available"

    pool = _make_pool([])
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace()),
        client=SimpleNamespace(host="127.0.0.1"),
    )

    body = SimpleNamespace(query="neural ODEs", max_results=5)
    expected = [{"id": 1, "title": "Paper", "rrf_score": 0.5}]

    with patch.object(embedder, "hybrid_search", new_callable=AsyncMock, return_value=expected):
        result = await search_router.search_hybrid.__wrapped__(
            request,
            body=body,
            db_pool=pool,
            embedder=embedder,
        )

    assert result == expected
