"""Tests for the tasks router — focused on DOM-C-07 paper-ownership guard."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import HTTPException
from httpx import ASGITransport

# ---------------------------------------------------------------------------
# Helpers (same pattern as test_le_endpoints.py)
# ---------------------------------------------------------------------------


def _make_pool_and_conn():
    """Return (pool, conn) with transaction context-manager support."""
    conn = AsyncMock()
    txn_cm = MagicMock()
    txn_cm.__aenter__ = AsyncMock(return_value=txn_cm)
    txn_cm.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn_cm)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx
    return pool, conn


@pytest.fixture()
def _app():
    """Minimal learning_engine app with mocked DB and disabled auth/rate-limit."""
    from jarvis_common import verify_api_key
    from jarvis_common.auth import current_user_id_strict
    from learning_engine.deps import get_db_pool
    from learning_engine.main import app

    pool, conn = _make_pool_and_conn()
    app.state.db_pool = pool
    app.state.limiter.enabled = False

    app.dependency_overrides[get_db_pool] = lambda: pool
    app.dependency_overrides[verify_api_key] = lambda: None
    app.dependency_overrides[current_user_id_strict] = lambda: 10

    yield app, conn

    app.dependency_overrides.clear()
    app.state.limiter.enabled = True


# ---------------------------------------------------------------------------
# DOM-C-07 — link_paper_to_task paper-ownership guard
# ---------------------------------------------------------------------------


async def _async_user_10(_request):
    return 10


async def test_link_paper_to_task_rejects_unowned_paper(_app, monkeypatch):
    """DOM-C-07: link_paper_to_task enforces paper ownership before the FK insert.

    Setup:
    - Task belongs to user 10 (task ownership check passes).
    - Paper belongs to user 99 and is NOT in user 10's user_library.
    - assert_paper_ownership raises 403 → endpoint returns 403.
    """
    monkeypatch.setattr(
        "learning_engine.routers.tasks.current_user_id_strict",
        _async_user_10,
    )

    async def _deny(_conn, _paper_id, _user_id):
        raise HTTPException(status_code=403, detail="paper not owned by current user")

    monkeypatch.setattr(
        "learning_engine.routers.tasks.assert_paper_ownership",
        _deny,
    )

    app, conn = _app
    # Task ownership check succeeds (task belongs to user 10)
    conn.fetchval.return_value = 5  # task id

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/tasks/5/papers",
            json={"paper_id": 99, "note": None},
        )

    assert resp.status_code == 403
    assert "not owned" in resp.json()["detail"]
