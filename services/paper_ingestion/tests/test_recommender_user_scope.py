"""Pure-unit test: user_id forwarding prevents cross-user vector leaks.

Verifies that _refresh_recommendations_for_user forwards user_id to
embedder.discover_from_seeds, preventing cross-user vector leaks.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from jarvis_common.testing import make_pool_and_conn
from paper_ingestion.ingestion.recommender import _refresh_recommendations_for_user

from tests._embedder_fakes import _dict_to_record, _make_embedder


def _make_app(pool: Any, embedder: Any) -> MagicMock:
    app = MagicMock()
    app.state.db_pool = pool
    app.state.embedder = embedder
    return app


@pytest.mark.asyncio
async def test_discover_from_seeds_receives_user_id() -> None:
    """discover_from_seeds must be called with user_id=<the requesting user>.

    DB call order (all via the same conn mock):
      acquire #1: conn.fetch → _read_weights (rows with key/value/user_id)
      acquire #2: conn.fetch → _get_starred_ids (rows with paper_id)
                  conn.fetch → projects query   (rows with name/description)
                  conn.fetch → caller library   (rows with paper_id)
      acquire #3: conn.fetch → _filter_unread   (only reached if discover returns hits)
    """
    pool, conn = make_pool_and_conn()

    # Three fetch calls share the same conn mock; supply side_effects in order.
    conn.fetch = AsyncMock(
        side_effect=[
            # _read_weights: return default weights (empty = use defaults)
            [],
            # _get_starred_ids: one starred paper so discover_from_seeds is called
            [{"paper_id": 99}],
            # projects query: no active projects
            [],
            # caller library: no additional private papers
            [],
        ]
    )

    embedder = MagicMock()
    embedder.discover_from_seeds = AsyncMock(return_value=[])

    app = _make_app(pool, embedder)

    await _refresh_recommendations_for_user(app, user_id=42)

    embedder.discover_from_seeds.assert_called_once()
    _, kwargs = embedder.discover_from_seeds.call_args
    assert kwargs.get("user_id") == 42, (
        f"discover_from_seeds was not called with user_id=42; got kwargs={kwargs}"
    )


@pytest.mark.asyncio
async def test_project_query_is_scoped_to_user_id() -> None:
    """Projects fetched for recommendation must be filtered to the requesting user.

    If user B has an active project named 'secret-project', user A must not
    see recommendation explanations containing 'secret-project'.
    """
    pool, conn = make_pool_and_conn()

    # Simulate _read_weights, _get_starred_ids, projects, then caller library.
    conn.fetch = AsyncMock(
        side_effect=[
            [],  # _read_weights: defaults
            [],  # _get_starred_ids: no starred papers
            [{"name": "secret-project", "description": "user B only"}],  # projects
            [],  # caller library
        ]
    )

    embedder = MagicMock()
    embedder.search_similar = AsyncMock(return_value=[])
    embedder.discover_from_seeds = AsyncMock(return_value=[])

    app = _make_app(pool, embedder)
    requesting_user_id = 7  # user A

    await _refresh_recommendations_for_user(app, requesting_user_id)

    projects_call = conn.fetch.call_args_list[2]
    bind_params = projects_call.args[1:]
    assert requesting_user_id in bind_params
    for call in embedder.search_similar.call_args_list:
        assert call.kwargs.get("user_id") == requesting_user_id


# ---------------------------------------------------------------------------
# Characterization: embedder.discover_from_seeds real body (Qdrant recommend
# path) — pins Pass-1 sampling, missing-seed embedding, dedup, and sort order.
# ---------------------------------------------------------------------------


class _FakeRecord:
    def __init__(self, rid: str) -> None:
        self.id = rid


class _FakeHit:
    def __init__(self, score: float, payload: Any) -> None:
        self.score = score
        self.payload = payload


class _FakeResponse:
    def __init__(self, points: list) -> None:
        self.points = points


def _make_fetch_pool(records: list) -> MagicMock:
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=records)
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    return pool


@pytest.mark.asyncio
async def test_discover_from_seeds_dedups_and_sorts() -> None:
    """Recommend hits are deduped by paper_id (best score kept) then sorted descending.

    Each surviving hit also carries the winning point's own ``chunk_index``.
    Both discovery endpoints keep only hits whose ``(paper_id, chunk_index)``
    pair still has a stored ``paper_chunks`` row, so a hit that reaches them
    without an index matches nothing and the endpoint answers empty.

    Verified: paper_ingestion/ingestion/search.py:799-804 (per-paper best hit)
    Verified: paper_ingestion/queries/chunk_liveness.py (a chunk carrying no
    chunk_index matches no stored key)
    """
    embedder = _make_embedder()
    embedder.qdrant.scroll = AsyncMock(return_value=([_FakeRecord("p1"), _FakeRecord("p2")], None))
    hits = [
        _FakeHit(0.7, {"paper_id": 5, "chunk_index": 11, "content": "c5-low"}),
        _FakeHit(0.9, {"paper_id": 5, "chunk_index": 4, "content": "c5-high"}),
        _FakeHit(0.8, {"paper_id": 7, "chunk_index": 0, "content": "c7"}),
        _FakeHit(0.6, {"paper_id": None, "chunk_index": 2, "content": "skip"}),
    ]
    embedder.qdrant.query_points = AsyncMock(return_value=_FakeResponse(hits))

    results = await embedder.discover_from_seeds([1], db_pool=AsyncMock(), limit=10)

    assert [r["paper_id"] for r in results] == [5, 7]
    assert results[0]["score"] == 0.9
    assert results[0]["content"] == "c5-high"
    assert [r.get("chunk_index") for r in results] == [4, 0], (
        "each result must carry the chunk_index of the point that won its paper; "
        f"got {[r.get('chunk_index') for r in results]}"
    )


@pytest.mark.asyncio
async def test_search_similar_carries_each_hit_chunk_index() -> None:
    """Every hit ``search_similar`` returns carries its own point's ``chunk_index``.

    ``GET /api/similar`` is the only consumer of this producer, and it keeps
    only hits whose ``(paper_id, chunk_index)`` pair still has a stored
    ``paper_chunks`` row. A hit reaching it without an index matches no stored
    key, so dropping the index here empties the endpoint for every caller. The
    contract tests cannot see that: they stub ``search_similar`` with the key
    already present.

    Verified: paper_ingestion/ingestion/search.py:256-263 (per-hit result dict)
    Verified: paper_ingestion/queries/chunk_liveness.py:83-88 (a chunk carrying
    no chunk_index matches no stored key)
    """
    embedder = _make_embedder()
    embedder.embed_texts = AsyncMock(return_value=[[0.1, 0.2, 0.3]])
    hits = [
        _FakeHit(0.9, {"paper_id": 5, "chunk_index": 4, "content": "c5"}),
        _FakeHit(0.8, {"paper_id": 7, "chunk_index": 0, "content": "c7"}),
    ]
    embedder.qdrant.query_points = AsyncMock(return_value=_FakeResponse(hits))

    results = await embedder.search_similar(query_text="a question", limit=5)

    assert [r["paper_id"] for r in results] == [5, 7]
    assert [r.get("chunk_index") for r in results] == [4, 0], (
        "each hit must carry the chunk_index of the point it came from; "
        f"got {[r.get('chunk_index') for r in results]}"
    )


@pytest.mark.asyncio
async def test_discover_from_seeds_embeds_missing_seeds() -> None:
    """Seeds absent from Qdrant are embedded from title+abstract and used as positives."""
    embedder = _make_embedder()
    embedder.qdrant.scroll = AsyncMock(return_value=([], None))  # seed missing from Qdrant
    pool = _make_fetch_pool([_dict_to_record({"id": 1, "title": "T", "abstract": "A"})])
    embedder.embed_texts = AsyncMock(return_value=[[0.1, 0.2, 0.3]])
    embedder.qdrant.query_points = AsyncMock(
        return_value=_FakeResponse([_FakeHit(0.9, {"paper_id": 5, "content": "c"})])
    )

    results = await embedder.discover_from_seeds([1], db_pool=pool, limit=10)

    embedder.embed_texts.assert_awaited_once()
    assert embedder.embed_texts.await_args.args[0] == ["T. A"]
    assert [r["paper_id"] for r in results] == [5]
