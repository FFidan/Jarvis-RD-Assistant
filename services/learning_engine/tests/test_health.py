"""Tests for GET /health endpoint of the learning_engine service."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from httpx import ASGITransport


def _make_mock_pool(raise_on_acquire: bool = False) -> MagicMock:
    """Return a mock asyncpg Pool.

    If *raise_on_acquire* is True the pool's acquire() context manager raises
    RuntimeError, simulating a DB connection failure.
    """
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=1)

    ctx = MagicMock()
    if raise_on_acquire:
        ctx.__aenter__ = AsyncMock(side_effect=RuntimeError("DB down"))
    else:
        ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)

    pool = MagicMock()
    pool.acquire.return_value = ctx
    return pool


@pytest.fixture()
def _app_with_deps():
    """Yield app with overridable state; clears overrides on teardown."""
    from app.main import app
    from jarvis_common import verify_api_key

    app.dependency_overrides[verify_api_key] = lambda: None
    yield app
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_health_returns_200_when_ok(_app_with_deps):
    """GET /health → 200 when all dependencies are reachable."""
    app = _app_with_deps
    app.state.db_pool = _make_mock_pool(raise_on_acquire=False)

    mock_http = AsyncMock()
    mock_http.get = AsyncMock(return_value=MagicMock(status_code=200))
    app.state.http_client = mock_http

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/health")

    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_health_returns_503_when_degraded(_app_with_deps):
    """GET /health → 503 when a dependency is unavailable."""
    app = _app_with_deps
    # Simulate DB failure
    app.state.db_pool = _make_mock_pool(raise_on_acquire=True)

    mock_http = AsyncMock()
    mock_http.get = AsyncMock(return_value=MagicMock(status_code=200))
    app.state.http_client = mock_http

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/health")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["checks"]["postgres"] == "unavailable"
