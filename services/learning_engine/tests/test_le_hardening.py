"""Tests for learning_engine hardening fixes.

Covers:
- M-4: Invalid status query parameter returns 400.
- H-1: dynamic_update returning None raises 404 (projects + milestones).
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException  # noqa: E402
from learning_engine.routers.milestones import update_milestone  # noqa: E402
from learning_engine.routers.projects import (  # noqa: E402
    _VALID_STATUSES,
    list_projects,
    update_project,
)
from learning_engine.routers.tasks import _VALID_TASK_STATUSES, list_tasks  # noqa: E402


def _fake_request():
    # WS-2A: routers read user_id from request.state.user_id (set by session middleware).
    # Tests pass a SimpleNamespace stand-in with user_id=None to mirror an
    # API-key-only / single-tenant caller.
    return SimpleNamespace(state=SimpleNamespace(user_id=None))


# ---------------------------------------------------------------------------
# M-4: status filter validation
# ---------------------------------------------------------------------------


async def test_list_projects_rejects_invalid_status() -> None:
    """list_projects raises 400 for a status value outside the valid set."""
    fake_pool = AsyncMock()

    with pytest.raises(HTTPException) as exc_info:
        await list_projects.__wrapped__(_fake_request(), status="nonexistent", db_pool=fake_pool)

    assert exc_info.value.status_code == 400
    assert "nonexistent" in exc_info.value.detail


def _make_pool_conn(return_value=None):
    """Create a pool mock whose acquire() context manager yields a conn mock."""
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=return_value or [])
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx
    return pool


async def test_list_projects_accepts_valid_statuses() -> None:
    """list_projects does not raise for each valid status value."""
    fake_pool = _make_pool_conn()

    for status in sorted(_VALID_STATUSES):
        rows = await list_projects.__wrapped__(_fake_request(), status=status, db_pool=fake_pool)
        assert rows == []


async def test_list_projects_accepts_none_status() -> None:
    """list_projects returns results when status is None (no filter)."""
    fake_pool = _make_pool_conn()

    rows = await list_projects.__wrapped__(_fake_request(), status=None, db_pool=fake_pool)
    assert rows == []


# ---------------------------------------------------------------------------
# H-25: task status filter validation
# ---------------------------------------------------------------------------


async def test_list_tasks_rejects_invalid_status() -> None:
    """list_tasks raises 400 for a status value outside the valid set."""
    fake_pool = AsyncMock()

    with pytest.raises(HTTPException) as exc_info:
        await list_tasks.__wrapped__(
            _fake_request(),
            project_id=1,
            status="nonexistent",
            db_pool=fake_pool,
        )

    assert exc_info.value.status_code == 400
    assert "nonexistent" in exc_info.value.detail


async def test_list_tasks_accepts_valid_statuses() -> None:
    """list_tasks does not raise for each valid task status value."""
    fake_conn = AsyncMock()
    fake_conn.fetchval = AsyncMock(return_value=1)  # project exists
    fake_conn.fetch = AsyncMock(return_value=[])

    fake_pool = AsyncMock()
    fake_pool.acquire = MagicMock(return_value=AsyncMock())
    fake_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=fake_conn)
    fake_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    for status in sorted(_VALID_TASK_STATUSES):
        rows = await list_tasks.__wrapped__(
            _fake_request(), project_id=1, status=status, db_pool=fake_pool
        )
        assert rows == []


async def test_list_tasks_accepts_none_status() -> None:
    """list_tasks returns results when status is None (no filter)."""
    fake_conn = AsyncMock()
    fake_conn.fetchval = AsyncMock(return_value=1)  # project exists
    fake_conn.fetch = AsyncMock(return_value=[])

    fake_pool = AsyncMock()
    fake_pool.acquire = MagicMock(return_value=AsyncMock())
    fake_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=fake_conn)
    fake_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    rows = await list_tasks.__wrapped__(
        _fake_request(), project_id=1, status=None, db_pool=fake_pool
    )
    assert rows == []


# ---------------------------------------------------------------------------
# H-1: dynamic_update None guard — projects
# ---------------------------------------------------------------------------


async def test_update_project_raises_404_when_dynamic_update_returns_none() -> None:
    """update_project raises 404 if the record is deleted between lock and update."""
    now = datetime.now()
    fake_existing = {
        "id": 1,
        "name": "P",
        "description": None,
        "status": "active",
        "deadline": None,
        "color": None,
        "created_at": now,
        "updated_at": now,
    }

    fake_conn = AsyncMock()
    fake_conn.fetchrow = AsyncMock(return_value=fake_existing)
    fake_conn.transaction = MagicMock(return_value=AsyncMock())

    fake_pool = AsyncMock()
    fake_pool.acquire = MagicMock(return_value=AsyncMock())
    fake_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=fake_conn)
    fake_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    fake_txn = AsyncMock()
    fake_conn.transaction = MagicMock(return_value=fake_txn)
    fake_txn.__aenter__ = AsyncMock(return_value=fake_txn)
    fake_txn.__aexit__ = AsyncMock(return_value=False)

    body = MagicMock()
    body.model_dump = MagicMock(return_value={"name": "Updated"})

    with patch(
        "learning_engine.routers.projects.dynamic_update", new_callable=AsyncMock
    ) as mock_du:
        mock_du.return_value = None

        with pytest.raises(HTTPException) as exc_info:
            await update_project.__wrapped__(
                _fake_request(), project_id=1, body=body, db_pool=fake_pool
            )

        assert exc_info.value.status_code == 404
        assert "deleted during update" in exc_info.value.detail


# ---------------------------------------------------------------------------
# H-1: dynamic_update None guard — milestones
# ---------------------------------------------------------------------------


async def test_update_milestone_raises_404_when_dynamic_update_returns_none() -> None:
    """update_milestone raises 404 if the record is deleted between lock and update."""
    now = datetime.now()
    fake_existing = {
        "id": 10,
        "project_id": 1,
        "name": "M",
        "deadline": None,
        "description": None,
        "completed": False,
        "completed_at": None,
        "created_at": now,
    }

    fake_conn = AsyncMock()
    fake_conn.fetchrow = AsyncMock(return_value=fake_existing)

    fake_pool = AsyncMock()
    fake_pool.acquire = MagicMock(return_value=AsyncMock())
    fake_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=fake_conn)
    fake_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    fake_txn = AsyncMock()
    fake_conn.transaction = MagicMock(return_value=fake_txn)
    fake_txn.__aenter__ = AsyncMock(return_value=fake_txn)
    fake_txn.__aexit__ = AsyncMock(return_value=False)

    body = MagicMock()
    body.model_dump = MagicMock(return_value={"name": "Updated Milestone"})

    with patch(
        "learning_engine.routers.milestones.dynamic_update", new_callable=AsyncMock
    ) as mock_du:
        mock_du.return_value = None

        with pytest.raises(HTTPException) as exc_info:
            await update_milestone.__wrapped__(
                _fake_request(),
                milestone_id=10,
                body=body,
                db_pool=fake_pool,
            )

        assert exc_info.value.status_code == 404
        assert "deleted during update" in exc_info.value.detail
