"""Tests for seed-based paper discovery (Embedder.discover_from_seeds)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from app.embedder import COLLECTION_NAME, Embedder

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_embedder() -> Embedder:
    """Create an Embedder with mocked HTTP and Qdrant clients."""
    mock_http = AsyncMock()
    mock_qdrant = AsyncMock()
    return Embedder(mock_http, mock_qdrant)


def _dict_to_record(d: dict) -> MagicMock:
    """Simulate an asyncpg.Record with dict-style access."""
    rec = MagicMock()
    rec.__getitem__ = lambda self, key: d[key]
    rec.keys = lambda: d.keys()
    return rec


def _make_pool_with_fetchrow(fetchrow_values: list[dict | None]) -> AsyncMock:
    """Create a mock pool where each acquire().fetchrow() returns the next value."""
    call_idx = 0

    async def _fetchrow(*args, **kwargs):
        nonlocal call_idx
        if call_idx < len(fetchrow_values):
            val = fetchrow_values[call_idx]
            call_idx += 1
            return _dict_to_record(val) if val else None
        return None

    conn = AsyncMock()
    conn.fetchrow = _fetchrow
    conn.fetch = AsyncMock(return_value=[])

    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = AsyncMock()
    pool.acquire = MagicMock(return_value=ctx)
    return pool


def _make_qdrant_record(point_id: str, payload: dict, score: float = 0.0) -> MagicMock:
    """Create a mock Qdrant point/record."""
    rec = MagicMock()
    rec.id = point_id
    rec.payload = payload
    rec.score = score
    return rec


def _make_scroll_record(point_id: str) -> MagicMock:
    """Create a minimal Qdrant scroll record (no payload needed)."""
    rec = MagicMock()
    rec.id = point_id
    return rec


# ---------------------------------------------------------------------------
# Test 1: Correct point IDs passed to RecommendInput.positive
# ---------------------------------------------------------------------------


async def test_discover_correct_positive_ids():
    """Scroll returns point IDs, and those are passed to RecommendInput.positive."""
    embedder = _make_embedder()
    seed_ids = [1, 2]

    # Seed 1 has 3 chunks, seed 2 has 2 chunks
    seed1_records = [_make_scroll_record(f"uuid-1-{i}") for i in range(3)]
    seed2_records = [_make_scroll_record(f"uuid-2-{i}") for i in range(2)]

    scroll_call = 0

    async def _scroll(**kwargs):
        nonlocal scroll_call
        scroll_call += 1
        if scroll_call == 1:
            return (seed1_records, None)
        return (seed2_records, None)

    embedder.qdrant.scroll = _scroll

    # query_points returns empty results
    qp_response = MagicMock()
    qp_response.points = []
    embedder.qdrant.query_points = AsyncMock(return_value=qp_response)

    pool = _make_pool_with_fetchrow([])

    await embedder.discover_from_seeds(seed_ids, pool, limit=5)

    # Check that query_points was called with the right positive IDs
    qp_call = embedder.qdrant.query_points.call_args
    query_obj = qp_call.kwargs["query"]
    positive_ids = query_obj.recommend.positive

    # All 5 point IDs should be present (3 from seed 1 + 2 from seed 2)
    expected = [f"uuid-1-{i}" for i in range(3)] + [f"uuid-2-{i}" for i in range(2)]
    assert positive_ids == expected


# ---------------------------------------------------------------------------
# Test 2: Seed paper_ids excluded via must_not filter
# ---------------------------------------------------------------------------


async def test_discover_excludes_seed_papers():
    """Seed paper_ids are excluded from results via must_not filter."""

    embedder = _make_embedder()
    seed_ids = [10, 20]

    records = [_make_scroll_record("pt-1")]
    embedder.qdrant.scroll = AsyncMock(return_value=(records, None))

    qp_response = MagicMock()
    qp_response.points = []
    embedder.qdrant.query_points = AsyncMock(return_value=qp_response)

    pool = _make_pool_with_fetchrow([])

    await embedder.discover_from_seeds(seed_ids, pool, limit=5)

    qp_call = embedder.qdrant.query_points.call_args
    query_filter = qp_call.kwargs["query_filter"]

    # Check must_not contains a FieldCondition excluding seed paper_ids
    assert len(query_filter.must_not) == 1
    fc = query_filter.must_not[0]
    assert fc.key == "paper_id"
    assert set(fc.match.any) == {10, 20}


# ---------------------------------------------------------------------------
# Test 3: Dedup by paper_id keeps highest score
# ---------------------------------------------------------------------------


async def test_discover_dedup_keeps_highest_score():
    """When multiple chunks from the same paper match, keep highest score."""
    embedder = _make_embedder()
    seed_ids = [1]

    records = [_make_scroll_record("pt-1")]
    embedder.qdrant.scroll = AsyncMock(return_value=(records, None))

    # Return two chunks from paper_id=99 with different scores
    hit_low = _make_qdrant_record(
        "hit-1", {"paper_id": 99, "content": "low score chunk"}, score=0.6
    )
    hit_high = _make_qdrant_record(
        "hit-2", {"paper_id": 99, "content": "high score chunk"}, score=0.9
    )
    hit_other = _make_qdrant_record(
        "hit-3", {"paper_id": 50, "content": "other paper chunk"}, score=0.7
    )

    qp_response = MagicMock()
    qp_response.points = [hit_low, hit_high, hit_other]
    embedder.qdrant.query_points = AsyncMock(return_value=qp_response)

    pool = _make_pool_with_fetchrow([])

    results = await embedder.discover_from_seeds(seed_ids, pool, limit=10)

    # Should have 2 papers (99 and 50), not 3
    assert len(results) == 2
    paper_99 = next(r for r in results if r["paper_id"] == 99)
    assert paper_99["score"] == 0.9
    assert paper_99["content"] == "high score chunk"


# ---------------------------------------------------------------------------
# Test 4: Fallback — unembedded seed uses title+abstract embedding
# ---------------------------------------------------------------------------


async def test_discover_fallback_embeds_title_abstract():
    """When a seed has no chunks in Qdrant, embed title+abstract as a vector."""
    embedder = _make_embedder()
    seed_ids = [42]

    # Scroll returns empty — no chunks for this seed
    embedder.qdrant.scroll = AsyncMock(return_value=([], None))

    # Mock embed_texts to return a fake vector
    fake_vector = [0.1] * 768
    embedder.embed_texts = AsyncMock(return_value=[fake_vector])

    qp_response = MagicMock()
    qp_response.points = []
    embedder.qdrant.query_points = AsyncMock(return_value=qp_response)

    pool = _make_pool_with_fetchrow([
        {"title": "My Paper Title", "abstract": "The abstract text"},
    ])

    await embedder.discover_from_seeds(seed_ids, pool, limit=5)

    # embed_texts should have been called with title + abstract
    embedder.embed_texts.assert_awaited_once_with(["My Paper Title. The abstract text"])

    # The raw vector should be passed as a positive example
    qp_call = embedder.qdrant.query_points.call_args
    positive = qp_call.kwargs["query"].recommend.positive
    assert len(positive) == 1
    assert positive[0] == fake_vector


# ---------------------------------------------------------------------------
# Test 5: Empty seeds — all unembedded, no abstract → empty result
# ---------------------------------------------------------------------------


async def test_discover_empty_seeds_no_content():
    """Seeds with no Qdrant chunks and no title/abstract return empty list."""
    embedder = _make_embedder()
    seed_ids = [100]

    # Scroll returns empty
    embedder.qdrant.scroll = AsyncMock(return_value=([], None))

    # DB returns paper with no title and no abstract
    pool = _make_pool_with_fetchrow([
        {"title": "", "abstract": ""},
    ])

    results = await embedder.discover_from_seeds(seed_ids, pool, limit=5)

    # No positive examples → should return empty list without calling query_points
    assert results == []


# ---------------------------------------------------------------------------
# Test 6: Evenly spaced sampling when many chunks
# ---------------------------------------------------------------------------


async def test_discover_samples_evenly():
    """When a seed has more chunks than max_points_per_seed, sample evenly."""
    embedder = _make_embedder()
    seed_ids = [1]

    # 20 chunks for seed 1
    all_records = [_make_scroll_record(f"uuid-{i}") for i in range(20)]
    embedder.qdrant.scroll = AsyncMock(return_value=(all_records, None))

    qp_response = MagicMock()
    qp_response.points = []
    embedder.qdrant.query_points = AsyncMock(return_value=qp_response)

    pool = _make_pool_with_fetchrow([])

    await embedder.discover_from_seeds(seed_ids, pool, limit=5, max_points_per_seed=5)

    qp_call = embedder.qdrant.query_points.call_args
    positive_ids = qp_call.kwargs["query"].recommend.positive

    # Should have exactly 5 sampled IDs (not all 20)
    assert len(positive_ids) == 5
    # Check they're evenly spaced: step = 20/5 = 4.0
    # Indices: int(0*4)=0, int(1*4)=4, int(2*4)=8, int(3*4)=12, int(4*4)=16
    expected = ["uuid-0", "uuid-4", "uuid-8", "uuid-12", "uuid-16"]
    assert positive_ids == expected


# ---------------------------------------------------------------------------
# Test 7: Score threshold and limit are forwarded to query_points
# ---------------------------------------------------------------------------


async def test_discover_forwards_params():
    """score_threshold and limit are passed through to query_points."""
    embedder = _make_embedder()
    seed_ids = [1]

    records = [_make_scroll_record("pt-1")]
    embedder.qdrant.scroll = AsyncMock(return_value=(records, None))

    qp_response = MagicMock()
    qp_response.points = []
    embedder.qdrant.query_points = AsyncMock(return_value=qp_response)

    pool = _make_pool_with_fetchrow([])

    await embedder.discover_from_seeds(
        seed_ids, pool, limit=7, score_threshold=0.8
    )

    qp_call = embedder.qdrant.query_points.call_args
    assert qp_call.kwargs["score_threshold"] == 0.8
    # limit is multiplied by 5 for pre-dedup headroom
    assert qp_call.kwargs["limit"] == 35
    assert qp_call.kwargs["collection_name"] == COLLECTION_NAME
    assert qp_call.kwargs["with_payload"] is True
