"""Pure-unit test for audit finding W1-D1-001.

Verifies that _refresh_recommendations_for_user forwards user_id to
embedder.discover_from_seeds, preventing cross-user vector leaks.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from jarvis_common.testing import make_pool_and_conn
from paper_ingestion.ingestion.recommender import _refresh_recommendations_for_user


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

    # Simulate the conn.fetch chain in order: _read_weights, _get_starred_ids, projects query
    conn.fetch = AsyncMock(
        side_effect=[
            [],  # _read_weights: defaults
            [],  # _get_starred_ids: no starred papers
            [{"name": "secret-project", "description": "user B only"}],  # projects
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
