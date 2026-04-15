"""Tests for hybrid_search pagination (offset parameter — BE-003).

Verifies that page 2+ of /api/papers/search returns correct, non-overlapping
results by applying offset *inside* hybrid_search after RRF fusion rather than
via a client-side slice.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.embedder import Embedder

# ---------------------------------------------------------------------------
# Helpers (same pattern as test_hybrid_search.py)
# ---------------------------------------------------------------------------


def _make_embedder() -> Embedder:
    mock_http = AsyncMock()
    mock_qdrant = AsyncMock()
    return Embedder(mock_http, mock_qdrant)


def _dict_to_record(d: dict) -> MagicMock:
    rec = MagicMock()
    rec.__getitem__ = lambda self, key: d[key]
    rec.keys = lambda: d.keys()
    return rec


def _make_bm25_rows(n: int) -> list[dict]:
    """Generate n synthetic BM25 rows with descending scores (id 1..n)."""
    return [
        {
            "id": i,
            "title": f"Paper {i}",
            "authors": [f"Author {i}"],
            "url": f"https://example.com/{i}",
            "abstract": f"Abstract {i}",
            "published_date": None,
            # Descending score so id=1 is rank 1
            "bm25_score": 1.0 - (i - 1) * 0.01,
        }
        for i in range(1, n + 1)
    ]


def _make_pool_with_rows(rows: list[dict]) -> AsyncMock:
    """Pool that returns the given rows for every fetch call."""
    records = [_dict_to_record(r) for r in rows]
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=records)
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = AsyncMock()
    pool.acquire = MagicMock(return_value=ctx)
    return pool


# ---------------------------------------------------------------------------
# Pagination tests: 15-item result set, pages of 5
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pagination_page1_returns_first_5():
    """offset=0, limit=5 → ids 1..5."""
    embedder = _make_embedder()
    pool = _make_pool_with_rows(_make_bm25_rows(15))

    with patch.object(embedder, "search_chunks_global", new_callable=AsyncMock, return_value=[]):
        results = await embedder.hybrid_search("test", pool, limit=5, offset=0)

    assert len(results) == 5
    ids = [r["id"] for r in results]
    assert ids == [1, 2, 3, 4, 5], f"Expected ids 1-5, got {ids}"


@pytest.mark.asyncio
async def test_pagination_page2_returns_next_5():
    """offset=5, limit=5 → ids 6..10 (no overlap with page 1)."""
    embedder = _make_embedder()
    pool = _make_pool_with_rows(_make_bm25_rows(15))

    with patch.object(embedder, "search_chunks_global", new_callable=AsyncMock, return_value=[]):
        results = await embedder.hybrid_search("test", pool, limit=5, offset=5)

    assert len(results) == 5
    ids = [r["id"] for r in results]
    assert ids == [6, 7, 8, 9, 10], f"Expected ids 6-10, got {ids}"


@pytest.mark.asyncio
async def test_pagination_page3_returns_last_5():
    """offset=10, limit=5 → ids 11..15."""
    embedder = _make_embedder()
    pool = _make_pool_with_rows(_make_bm25_rows(15))

    with patch.object(embedder, "search_chunks_global", new_callable=AsyncMock, return_value=[]):
        results = await embedder.hybrid_search("test", pool, limit=5, offset=10)

    assert len(results) == 5
    ids = [r["id"] for r in results]
    assert ids == [11, 12, 13, 14, 15], f"Expected ids 11-15, got {ids}"


@pytest.mark.asyncio
async def test_pagination_page4_returns_empty():
    """offset=15 past end of 15-item set → empty list."""
    embedder = _make_embedder()
    pool = _make_pool_with_rows(_make_bm25_rows(15))

    with patch.object(embedder, "search_chunks_global", new_callable=AsyncMock, return_value=[]):
        results = await embedder.hybrid_search("test", pool, limit=5, offset=15)

    assert results == [], f"Expected empty list, got {results}"


@pytest.mark.asyncio
async def test_pagination_all_pages_no_overlap():
    """Pages 1-3 together cover all 15 results with no duplicates."""
    embedder = _make_embedder()

    all_ids: list[int] = []
    for offset in [0, 5, 10]:
        pool = _make_pool_with_rows(_make_bm25_rows(15))
        with patch.object(
            embedder, "search_chunks_global", new_callable=AsyncMock, return_value=[]
        ):
            page = await embedder.hybrid_search("test", pool, limit=5, offset=offset)
        all_ids.extend(r["id"] for r in page)

    assert len(all_ids) == 15, f"Expected 15 total, got {len(all_ids)}"
    assert len(set(all_ids)) == 15, f"Duplicate ids detected: {sorted(all_ids)}"
    assert set(all_ids) == set(range(1, 16)), f"Missing ids: {set(range(1, 16)) - set(all_ids)}"


# ---------------------------------------------------------------------------
# Backward-compatibility: offset=0 is the default (existing callers unaffected)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_offset_default_zero_is_backward_compatible():
    """Calling hybrid_search without offset= still returns the first page."""
    embedder = _make_embedder()
    pool = _make_pool_with_rows(_make_bm25_rows(5))

    with patch.object(embedder, "search_chunks_global", new_callable=AsyncMock, return_value=[]):
        results = await embedder.hybrid_search("test", pool, limit=3)

    assert len(results) == 3
    assert results[0]["id"] == 1


# ---------------------------------------------------------------------------
# search.py caller: offset not passed → still works (no regression)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_router_caller_no_offset_unchanged():
    """The search.py caller (no offset arg) returns limit results from page 1."""
    embedder = _make_embedder()
    pool = _make_pool_with_rows(_make_bm25_rows(10))

    with patch.object(embedder, "search_chunks_global", new_callable=AsyncMock, return_value=[]):
        # Simulates: embedder.hybrid_search(body.query, db_pool, limit=body.max_results)
        results = await embedder.hybrid_search("query", pool, limit=5)

    assert len(results) == 5
    assert [r["id"] for r in results] == [1, 2, 3, 4, 5]
