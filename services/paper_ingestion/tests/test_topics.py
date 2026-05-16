"""RBAC tests for topic CUD endpoints (PI-C).

``topics`` is an instance-global table (no ``user_id`` column) whose rows
cascade into ``paper_topics``, so create/update/delete must be admin-only. Any
authenticated non-admin tenant could otherwise mutate the global catalogue.

- non-admin session → 403 on each CUD op
- admin session     → success
- read endpoints (GET /api/topics, subscription endpoints) are unaffected
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import HTTPException
from httpx import ASGITransport


class FakeRecord(dict):
    def keys(self):
        return super().keys()


def _make_pool_and_conn():
    conn = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx
    return pool, conn


def _topic_row(id=1, name="ML"):
    return FakeRecord(
        id=id,
        name=name,
        query_terms=["machine learning"],
        category="ai",
        description=None,
        enabled=True,
        created_at=datetime.now(UTC),
    )


def _client(app):
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture()
def _app(request):
    """Full app with mocked DB, bypassed API-key auth, limiter off.

    ``request.param`` (parametrized via indirect) is the simulated session
    role: ``None``/``"user"`` exercise the rejection path (the real
    ``require_admin`` raises 403 when ``request.state.user_role != 'admin'``),
    ``"admin"`` exercises the success path.

    The ``require_admin`` override resolves the role directly rather than via a
    FastAPI-injected ``Request`` dependency: an injected ``Request`` param on
    the override callable is misresolved by FastAPI as a query parameter once
    the slowapi-wrapped endpoint also declares a body.
    """
    from jarvis_common import verify_api_key
    from jarvis_common.auth import require_admin
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app
    from paper_ingestion.routers import topics as topics_router

    user_role = getattr(request, "param", None)

    mock_pool, conn = _make_pool_and_conn()
    conn.fetch.return_value = [_topic_row()]
    conn.fetchrow.return_value = _topic_row(id=3, name="New Topic")

    app.state.db_pool = mock_pool
    app.state.limiter.enabled = False
    app.dependency_overrides[get_db_pool] = lambda: mock_pool
    app.dependency_overrides[verify_api_key] = lambda: None

    async def _patched_require_admin() -> None:
        if user_role != "admin":
            raise HTTPException(status_code=403, detail="Admin role required")

    app.dependency_overrides[require_admin] = _patched_require_admin

    yield app, conn, topics_router

    app.dependency_overrides.clear()
    app.state.limiter.enabled = True


_VALID_BODY = {"name": "Robotics", "query_terms": ["robotics"]}


# ---------------------------------------------------------------------------
# Non-admin → 403 on every CUD op
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("_app", ["user"], indirect=True)
async def test_create_topic_rejects_non_admin(_app):
    app, conn, _ = _app
    async with _client(app) as c:
        resp = await c.post("/api/topics", json=_VALID_BODY)
    assert resp.status_code == 403, resp.text
    conn.fetchrow.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("_app", ["user"], indirect=True)
async def test_update_topic_rejects_non_admin(_app):
    app, conn, _ = _app
    async with _client(app) as c:
        resp = await c.put("/api/topics/1", json={"name": "X"})
    assert resp.status_code == 403, resp.text
    conn.fetchrow.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("_app", ["user"], indirect=True)
async def test_delete_topic_rejects_non_admin(_app):
    app, conn, _ = _app
    async with _client(app) as c:
        resp = await c.delete("/api/topics/1")
    assert resp.status_code == 403, resp.text
    conn.execute.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("_app", [None], indirect=True)
async def test_create_topic_rejects_no_session(_app):
    """API-key-only caller (no session ⇒ no user_role) is not an admin."""
    app, conn, _ = _app
    async with _client(app) as c:
        resp = await c.post("/api/topics", json=_VALID_BODY)
    assert resp.status_code == 403, resp.text
    conn.fetchrow.assert_not_called()


# ---------------------------------------------------------------------------
# Admin → success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("_app", ["admin"], indirect=True)
async def test_create_topic_allows_admin(_app):
    app, conn, _ = _app
    async with _client(app) as c:
        resp = await c.post("/api/topics", json=_VALID_BODY)
    assert resp.status_code == 201, resp.text
    assert resp.json()["name"] == "New Topic"


@pytest.mark.asyncio
@pytest.mark.parametrize("_app", ["admin"], indirect=True)
async def test_delete_topic_allows_admin(_app, monkeypatch):
    app, conn, topics_router = _app
    delete_or_404 = AsyncMock()
    log_audit = AsyncMock()
    monkeypatch.setattr(topics_router, "delete_or_404", delete_or_404)
    monkeypatch.setattr(topics_router, "log_audit", log_audit)
    async with _client(app) as c:
        resp = await c.delete("/api/topics/1")
    assert resp.status_code == 204, resp.text
    delete_or_404.assert_awaited_once()


# ---------------------------------------------------------------------------
# Read endpoints are unaffected by the admin gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("_app", [None, "user"], indirect=True)
async def test_list_topics_unaffected_by_admin_gate(_app):
    app, conn, _ = _app
    async with _client(app) as c:
        resp = await c.get("/api/topics")
    assert resp.status_code == 200, resp.text
    assert len(resp.json()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("_app", ["user"], indirect=True)
async def test_subscriptions_endpoint_unaffected_by_admin_gate(_app, monkeypatch):
    """A non-admin authenticated user can still list subscriptions."""
    app, conn, topics_router = _app
    monkeypatch.setattr(topics_router, "current_user_id_strict", AsyncMock(return_value=42))
    conn.fetch.return_value = [{"topic_id": 5}]
    async with _client(app) as c:
        resp = await c.get("/api/topics/subscriptions")
    assert resp.status_code == 200, resp.text
    assert resp.json() == [5]
