"""Topic subscription router tests.

Covers GET /api/topics/subscriptions, PUT /api/topics/{id}/subscribe,
DELETE /api/topics/{id}/subscribe.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest
from httpx import ASGITransport

from tests.conftest import _make_pool_and_conn


@pytest.fixture()
def _app():
    """FastAPI app with mocked DB pool, bypassed API-key auth, rate limiter off."""
    from jarvis_common import verify_api_key
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app
    from paper_ingestion.routers import topics as topics_router

    mock_pool, conn = _make_pool_and_conn()
    app.dependency_overrides[get_db_pool] = lambda: mock_pool
    app.dependency_overrides[verify_api_key] = lambda: None
    app.state.limiter.enabled = False

    # Inject a real user_id via current_user_id_or_none.
    orig = topics_router.current_user_id_strict
    topics_router.current_user_id_strict = AsyncMock(return_value=42)

    yield app, conn

    topics_router.current_user_id_strict = orig
    app.dependency_overrides.clear()
    app.state.limiter.enabled = True


@pytest.fixture()
def _unauthed_app():
    """App where current_user_id_or_none returns None (unauthenticated)."""
    from jarvis_common import verify_api_key
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app
    from paper_ingestion.routers import topics as topics_router

    mock_pool, conn = _make_pool_and_conn()
    app.dependency_overrides[get_db_pool] = lambda: mock_pool
    app.dependency_overrides[verify_api_key] = lambda: None
    app.state.limiter.enabled = False

    orig = topics_router.current_user_id_strict
    topics_router.current_user_id_strict = AsyncMock(return_value=None)

    yield app, conn

    topics_router.current_user_id_strict = orig
    app.dependency_overrides.clear()
    app.state.limiter.enabled = True


@pytest.mark.asyncio
async def test_subscribe_then_list(_app):
    """PUT subscribe → GET subscriptions returns topic_id."""
    app, conn = _app
    conn.fetchval = AsyncMock(return_value=1)  # topic exists
    conn.execute = AsyncMock(return_value="INSERT 0 1")
    conn.fetch = AsyncMock(return_value=[{"topic_id": 5}])

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        put_resp = await client.put("/api/topics/5/subscribe")
        assert put_resp.status_code == 204

        get_resp = await client.get("/api/topics/subscriptions")
        assert get_resp.status_code == 200
        assert get_resp.json() == [5]


@pytest.mark.asyncio
async def test_subscribe_idempotent(_app):
    """Subscribing twice is idempotent — ON CONFLICT DO NOTHING, list has one row."""
    app, conn = _app
    conn.fetchval = AsyncMock(return_value=1)
    conn.execute = AsyncMock(return_value="INSERT 0 0")
    conn.fetch = AsyncMock(return_value=[{"topic_id": 7}])

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.put("/api/topics/7/subscribe")
        resp = await client.put("/api/topics/7/subscribe")
        assert resp.status_code == 204

        get_resp = await client.get("/api/topics/subscriptions")
        assert get_resp.json() == [7]


@pytest.mark.asyncio
async def test_unsubscribe_clears_subscription(_app):
    """DELETE subscribe → list is empty."""
    app, conn = _app
    conn.execute = AsyncMock(return_value="DELETE 1")
    conn.fetch = AsyncMock(return_value=[])

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        del_resp = await client.delete("/api/topics/5/subscribe")
        assert del_resp.status_code == 204

        get_resp = await client.get("/api/topics/subscriptions")
        assert get_resp.json() == []


@pytest.mark.asyncio
async def test_subscribe_nonexistent_topic_returns_404(_app):
    """PUT subscribe to a topic that doesn't exist returns 404."""
    app, conn = _app
    conn.fetchval = AsyncMock(return_value=None)  # topic not found

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.put("/api/topics/999/subscribe")
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_subscribe_without_auth_returns_401(_unauthed_app):
    """PUT subscribe without authentication returns 401."""
    app, _conn = _unauthed_app

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.put("/api/topics/1/subscribe")
        assert resp.status_code == 401


@pytest.mark.asyncio
async def test_list_subscriptions_without_auth_returns_401(_unauthed_app):
    """GET subscriptions without authentication returns 401."""
    app, _conn = _unauthed_app

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/topics/subscriptions")
        assert resp.status_code == 401
