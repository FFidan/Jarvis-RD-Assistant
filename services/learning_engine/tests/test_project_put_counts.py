"""Unit tests for PUT /api/projects/{id} returning real counts.

Verifies that _fetch_project_with_counts is used after the update so
paper_count / open_question_count reflect actual aggregation rather than
the model defaults of 0.
"""

from __future__ import annotations

from datetime import datetime, UTC
from unittest.mock import AsyncMock

import httpx
import pytest
from httpx import ASGITransport

from jarvis_common.testing import FakeRecord, make_pool_and_conn

# Fixed stand-in for "now": row timestamps must not depend on when the suite runs.
_FIXED_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

_PROJECT_ROW = FakeRecord(
    id=7,
    name="Updated Name",
    description=None,
    status="active",
    deadline=None,
    color=None,
    user_id=10,
    created_at=_FIXED_NOW,
    updated_at=_FIXED_NOW,
)

_COUNTS_ROW = FakeRecord(
    total_tasks=3,
    done_tasks=1,
    total_milestones=2,
    completed_milestones=0,
    paper_count=5,
    open_question_count=4,
)


@pytest.fixture()
def _app_put():
    """LE app with mocked DB wired for update_project; auth + rate-limit disabled."""
    from jarvis_common import verify_api_key
    from jarvis_common.auth import current_user_id_strict
    from learning_engine.deps import get_db_pool
    from learning_engine.main import app

    conn = AsyncMock()

    # transaction context manager
    txn_cm = type(
        "_Txn",
        (),
        {
            "__aenter__": AsyncMock(return_value=None),
            "__aexit__": AsyncMock(return_value=False),
        },
    )()
    conn.transaction = lambda: txn_cm

    # fetchrow call sequence:
    # 1st: SELECT FOR UPDATE (lock check in update_project)  → _PROJECT_ROW
    # 2nd: dynamic_update RETURNING * (inside dynamic_update)  → _PROJECT_ROW
    # 3rd: _fetch_project_with_counts SELECT *               → _PROJECT_ROW
    # 4th: _fetch_project_with_counts counts query           → _COUNTS_ROW
    conn.fetchrow = AsyncMock(side_effect=[_PROJECT_ROW, _PROJECT_ROW, _PROJECT_ROW, _COUNTS_ROW])
    # dynamic_update calls conn.execute or conn.fetchrow internally;
    # execute is a no-op here.
    conn.execute = AsyncMock(return_value=None)

    pool, _ = make_pool_and_conn(conn=conn, with_transaction=False)

    app.dependency_overrides[get_db_pool] = lambda: pool
    app.dependency_overrides[verify_api_key] = lambda: None
    app.dependency_overrides[current_user_id_strict] = lambda: 10
    app.state.limiter.enabled = False

    yield app

    app.dependency_overrides.clear()
    app.state.limiter.enabled = True


async def test_put_project_returns_real_counts(_app_put):
    """PUT /api/projects/{id} response must include real paper_count and open_question_count."""
    async with httpx.AsyncClient(
        transport=ASGITransport(app=_app_put), base_url="http://test"
    ) as client:
        resp = await client.put("/api/projects/7", json={"name": "Updated Name"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["paper_count"] == 5
    assert body["open_question_count"] == 4
