"""Tests for hybrid_search pagination (offset parameter — BE-003).

Verifies that page 2+ of /api/papers/search returns correct, non-overlapping
results by applying offset *inside* hybrid_search after RRF fusion rather than
via a client-side slice.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jarvis_common.testing import make_pool_and_conn

from tests._embedder_fakes import _dict_to_record, _make_embedder


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


def _make_pool_with_rows(rows: list[dict]) -> MagicMock:
    records = [_dict_to_record(r) for r in rows]
    return make_pool_and_conn(fetch_return=records, with_transaction=False)[0]


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


# ---------------------------------------------------------------------------
# Characterization: semantic-only papers get metadata via a second DB fetch,
# and user_id is threaded into both the BM25 and metadata queries.
# ---------------------------------------------------------------------------


def _make_pool_with_fetch_sequence(record_batches: list[list]) -> tuple[MagicMock, AsyncMock]:
    """Pool whose conn.fetch returns each batch in turn; returns (pool, conn)."""
    pool, conn = make_pool_and_conn(with_transaction=False)
    conn.fetch = AsyncMock(side_effect=record_batches)
    return pool, conn


@pytest.mark.asyncio
async def test_hybrid_semantic_only_paper_gets_metadata_fetched():
    """A paper found only in the semantic leg gets its metadata via the second DB fetch."""
    embedder = _make_embedder()
    bm25_records = [_dict_to_record(r) for r in _make_bm25_rows(1)]  # id=1
    semantic_meta = _dict_to_record(
        {
            "id": 42,
            "title": "Semantic Paper",
            "authors": ["A"],
            "url": "https://example.com/42",
            "abstract": "Abs 42",
            "published_date": None,
        }
    )
    pool, conn = _make_pool_with_fetch_sequence([bm25_records, [semantic_meta]])

    chunks = [{"paper_id": 42, "score": 0.9}]
    with patch.object(
        embedder, "search_chunks_global", new_callable=AsyncMock, return_value=chunks
    ):
        results = await embedder.hybrid_search("q", pool, limit=10, offset=0)

    ids = [r["id"] for r in results]
    assert ids == [1, 42]
    assert conn.fetch.await_count == 2  # BM25 fetch + semantic-only metadata fetch
    paper42 = next(r for r in results if r["id"] == 42)
    assert paper42["title"] == "Semantic Paper"
    assert paper42["bm25_rank"] is None
    assert paper42["semantic_rank"] == 1


@pytest.mark.asyncio
async def test_hybrid_user_scoped_threads_user_id_into_queries():
    """When user_id is set, BM25, metadata, and semantic scope use caller visibility."""
    embedder = _make_embedder()
    library_records = [_dict_to_record({"paper_id": 1}), _dict_to_record({"paper_id": 42})]
    bm25_records = [_dict_to_record(r) for r in _make_bm25_rows(1)]
    semantic_meta = _dict_to_record(
        {
            "id": 42,
            "title": "Semantic Paper",
            "authors": ["A"],
            "url": "https://example.com/42",
            "abstract": "Abs 42",
            "published_date": None,
        }
    )
    pool, conn = _make_pool_with_fetch_sequence([library_records, bm25_records, [semantic_meta]])

    chunks = [{"paper_id": 42, "score": 0.9}]
    search_global = AsyncMock(return_value=chunks)
    with patch.object(embedder, "search_chunks_global", search_global):
        await embedder.hybrid_search("q", pool, limit=10, offset=0, user_id=7)

    assert 7 in conn.fetch.await_args_list[0].args  # library visibility fetch scoped to user 7
    assert 7 in conn.fetch.await_args_list[1].args  # BM25 leg scoped to user 7
    assert 7 in conn.fetch.await_args_list[2].args  # semantic-only metadata scoped to user 7
    semantic_scope = search_global.await_args.kwargs.get("scope")
    assert semantic_scope is not None
    assert semantic_scope.user_id == 7
    assert semantic_scope.library_paper_ids == (1, 42)


@pytest.mark.asyncio
async def test_hybrid_global_search_does_not_fetch_library_scope():
    """Anonymous/global hybrid search must not add a user-library widening query."""
    embedder = _make_embedder()
    bm25_records = [_dict_to_record(r) for r in _make_bm25_rows(1)]
    pool, conn = _make_pool_with_fetch_sequence([bm25_records])

    search_global = AsyncMock(return_value=[])
    with patch.object(embedder, "search_chunks_global", search_global):
        await embedder.hybrid_search("q", pool, limit=10, offset=0, user_id=None)

    assert conn.fetch.await_count == 1
    semantic_scope = search_global.await_args.kwargs.get("scope")
    assert semantic_scope is not None
    assert semantic_scope.user_id is None
    assert semantic_scope.library_paper_ids == ()
