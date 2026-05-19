"""Tests for seed-based paper discovery (Embedder.discover_from_seeds)."""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock

import pytest
from paper_ingestion.embedder import COLLECTION_NAME

# ---------------------------------------------------------------------------
# Per-test stub for qdrant_client.models
# ---------------------------------------------------------------------------
# The discover_from_seeds tests inspect call_args on RecommendInput, FieldCondition,
# and MatchAny, which requires those to be MagicMock callables, not real Pydantic models.


@pytest.fixture(autouse=True)
def _stub_qdrant_models(monkeypatch):
    """Scope MagicMock stubs for qdrant_client.models to each individual test."""
    import types

    fake_qm = types.ModuleType("qdrant_client.models")
    for _attr in (
        "Distance",
        "FieldCondition",
        "Filter",
        "IsNullCondition",
        "MatchAny",
        "MatchValue",
        "PayloadField",
        "PointIdsList",
        "PointStruct",
        "VectorParams",
        "RecommendInput",
        "RecommendQuery",
        "RecommendStrategy",
    ):
        setattr(fake_qm, _attr, MagicMock())
    from types import SimpleNamespace

    fake_qm.Distance = SimpleNamespace(COSINE="cosine")
    fake_qm.RecommendStrategy = SimpleNamespace(AVERAGE_VECTOR="average")
    monkeypatch.setitem(sys.modules, "qdrant_client.models", fake_qm)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# D3-05/09: shared fakes — replaces _make_embedder/_dict_to_record duplicated 3×.
from tests._embedder_fakes import _dict_to_record, _make_embedder


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

    # qdrant_client.models classes are MagicMocks in tests — inspect call args directly.
    # RecommendInput(positive=all_positive) records the positional list.
    recommend_input_mock = sys.modules["qdrant_client.models"].RecommendInput
    positive_ids = recommend_input_mock.call_args.kwargs["positive"]

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

    # qdrant_client.models classes are MagicMocks — inspect call args directly.
    # Filter(must_not=[FieldCondition(key=..., match=MatchAny(any=seed_ids))])
    field_condition_mock = sys.modules["qdrant_client.models"].FieldCondition
    match_any_mock = sys.modules["qdrant_client.models"].MatchAny

    # FieldCondition is called once for the must_not exclusion filter
    fc_call = field_condition_mock.call_args
    assert fc_call.kwargs["key"] == "paper_id"

    # MatchAny is called with any=seed_paper_ids
    ma_call = match_any_mock.call_args
    assert set(ma_call.kwargs["any"]) == {10, 20}


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
    fake_vector = [0.1] * 1024
    embedder.embed_texts = AsyncMock(return_value=[fake_vector])

    qp_response = MagicMock()
    qp_response.points = []
    embedder.qdrant.query_points = AsyncMock(return_value=qp_response)

    # ING-002: fallback now uses batch fetch (conn.fetch) not fetchrow
    pool = _make_pool_with_fetchrow([])
    pool.acquire.return_value.__aenter__.return_value.fetch = AsyncMock(
        return_value=[
            _dict_to_record({"id": 42, "title": "My Paper Title", "abstract": "The abstract text"})
        ]
    )

    await embedder.discover_from_seeds(seed_ids, pool, limit=5)

    # embed_texts should have been called with title + abstract
    embedder.embed_texts.assert_awaited_once_with(["My Paper Title. The abstract text"])

    # The raw vector should be passed as a positive example.
    # RecommendInput is a MagicMock — inspect its call args directly.
    recommend_input_mock = sys.modules["qdrant_client.models"].RecommendInput
    positive = recommend_input_mock.call_args.kwargs["positive"]
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
    pool = _make_pool_with_fetchrow(
        [
            {"title": "", "abstract": ""},
        ]
    )

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

    # RecommendInput is a MagicMock — inspect call args directly.
    recommend_input_mock = sys.modules["qdrant_client.models"].RecommendInput
    positive_ids = recommend_input_mock.call_args.kwargs["positive"]

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

    await embedder.discover_from_seeds(seed_ids, pool, limit=7, score_threshold=0.8)

    qp_call = embedder.qdrant.query_points.call_args
    assert qp_call.kwargs["score_threshold"] == 0.8
    # limit is multiplied by 5 for pre-dedup headroom
    assert qp_call.kwargs["limit"] == 35
    assert qp_call.kwargs["collection_name"] == COLLECTION_NAME
    assert qp_call.kwargs["with_payload"] is True


# ---------------------------------------------------------------------------
# Test 8: Security — user_id scoping prevents cross-user vector leak (B-DISCOVER)
# ---------------------------------------------------------------------------


async def test_discover_passes_user_id_to_qdrant_filter():
    """Security: discover_from_seeds must pass user_id so Qdrant query is scoped.

    Pre-fix: calling with user_id= raises TypeError (param doesn't exist).
    Post-fix: user_id is accepted and _user_scope_filter is called, producing
    a Filter whose should-clauses include a FieldCondition on 'user_id'.
    """
    embedder = _make_embedder()
    seed_ids = [1]

    records = [_make_scroll_record("pt-1")]
    embedder.qdrant.scroll = AsyncMock(return_value=(records, None))

    qp_response = MagicMock()
    qp_response.points = []
    embedder.qdrant.query_points = AsyncMock(return_value=qp_response)

    pool = _make_pool_with_fetchrow([])

    # This call must NOT raise TypeError — user_id is an accepted parameter.
    await embedder.discover_from_seeds(seed_ids, pool, limit=5, user_id=42)

    # query_points must have been called with a query_filter that incorporates
    # the user scope.  _user_scope_filter(42) produces a Filter with should=[...].
    # In tests qdrant_client.models.Filter is a MagicMock, so inspect call_args.
    filter_mock = sys.modules["qdrant_client.models"].Filter
    # Filter is called at least twice: once for seed scroll, once for the
    # combined query filter.  The final call (query_filter) must have been
    # constructed with a 'must' list that includes the user-scope sub-filter.
    #
    # _user_scope_filter returns Filter(should=[...]).  The combined filter is:
    #   Filter(must=[<user_scope_filter>, ...], must_not=[...])
    # Verify that Filter was called with keyword 'must' containing at least one
    # element (the user_scope result) — proving the scope was wired in.
    query_filter_call = embedder.qdrant.query_points.call_args.kwargs["query_filter"]
    # The query_filter object was produced by a Filter(must=...) call.
    # Since Filter is a MagicMock, every call returns a new MagicMock instance.
    # We verify the combined filter was passed (not None).
    assert query_filter_call is not None

    # Additionally verify Filter was invoked with 'must' kwarg for the combined
    # filter (seed-exclusion only used must_not before; post-fix uses must too).
    filter_calls_kwargs = [c.kwargs for c in filter_mock.call_args_list]
    combined_calls = [kw for kw in filter_calls_kwargs if "must" in kw]
    assert combined_calls, (
        "Filter was never called with 'must' kwarg — user scope not wired into query_filter"
    )
