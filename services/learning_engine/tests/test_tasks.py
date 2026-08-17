"""Tests for the tasks router — focused on the paper-ownership guard."""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException
from httpx import ASGITransport

from tests.conftest import _make_pool_and_conn


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
# link_paper_to_task paper-ownership guard
# ---------------------------------------------------------------------------


async def _async_user_10(_request):
    return 10


@pytest.fixture()
def _app_with_txn():
    """Same as _app but returns the transaction context-manager too for introspection."""
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

    yield app, conn, conn.transaction.return_value  # expose txn_cm

    app.dependency_overrides.clear()
    app.state.limiter.enabled = True


# ---------------------------------------------------------------------------
# LE-OB3 — unlink_paper_from_task transaction wrapping + ownership guard
# ---------------------------------------------------------------------------


async def test_unlink_paper_from_task_success(_app_with_txn, monkeypatch):
    """Success path: task owned by caller + link exists → 204 and transaction entered."""
    app, conn, txn_cm = _app_with_txn
    audit = AsyncMock()
    monkeypatch.setattr("learning_engine.routers.tasks.log_audit", audit)
    conn.fetchval.return_value = 5  # task id (ownership check passes)
    conn.execute.return_value = "DELETE 1"

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.delete("/api/tasks/5/papers/99")

    assert resp.status_code == 204
    # Transaction context-manager must have been entered exactly once.
    txn_cm.__aenter__.assert_awaited_once()
    # execute is called for the DELETE within the route's transaction.
    assert conn.execute.await_count >= 1
    first_call_sql = conn.execute.call_args_list[0].args[0]
    assert "DELETE" in first_call_sql and "task_paper_links" in first_call_sql
    audit.assert_awaited_once()


async def test_unlink_paper_from_task_task_not_found(_app_with_txn):
    """Task not found / not owned by caller → 404, DELETE never called."""
    app, conn, txn_cm = _app_with_txn
    conn.fetchval.return_value = None  # ownership check fails

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.delete("/api/tasks/5/papers/99")

    assert resp.status_code == 404
    assert "Task not found" in resp.json()["detail"]
    conn.execute.assert_not_awaited()


async def test_unlink_paper_from_task_link_not_found(_app_with_txn):
    """Task owned by caller but link row absent → 404."""
    app, conn, txn_cm = _app_with_txn
    conn.fetchval.return_value = 5  # task ownership passes
    conn.execute.return_value = "DELETE 0"  # no rows deleted

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.delete("/api/tasks/5/papers/99")

    assert resp.status_code == 404
    assert "Link not found" in resp.json()["detail"]


async def test_link_paper_to_task_rejects_unowned_paper(_app, monkeypatch):
    """link_paper_to_task enforces paper ownership before the FK insert.

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
