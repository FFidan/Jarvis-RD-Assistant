"""Tests for GET /health and /health/internal endpoints of the learning_engine service.

Public /health (ζ4: SEC-H09):
- Returns only {"status": "ok"|"degraded"} — no dependency details exposed.
- HTTP 200 when all deps are reachable; HTTP 503 when any check fails.

Authenticated /health/internal:
- Returns full {status, service, checks} payload.
- Requires valid API key.

Also covers M26 regression: HealthCheckResponse importable from jarvis_common.
"""

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
def app_with_deps():
    """Yield app with overridable state; clears overrides on teardown."""
    from jarvis_common import verify_api_key
    from learning_engine.main import app

    app.dependency_overrides[verify_api_key] = lambda: None
    yield app
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Public /health tests (status-only, no auth required)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_returns_200_when_ok(app_with_deps):
    """GET /health → 200 and status='ok' — no dependency details exposed."""
    app = app_with_deps
    app.state.db_pool = _make_mock_pool(raise_on_acquire=False)

    mock_http = AsyncMock()
    mock_http.get = AsyncMock(return_value=MagicMock(status_code=200))
    app.state.http_client = mock_http

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    # Public endpoint must NOT expose internal details
    assert "service" not in body
    assert "checks" not in body


@pytest.mark.asyncio
async def test_health_returns_503_when_degraded(app_with_deps):
    """GET /health → 503 and status='degraded' — no checks dict (SEC-H09)."""
    app = app_with_deps
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
    # Public endpoint must NOT expose which dependency failed
    assert "checks" not in body


# ---------------------------------------------------------------------------
# /health/internal tests (full details, requires auth)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_internal_returns_full_details(app_with_deps):
    """GET /health/internal → 200 with {status, service, checks} when authed."""
    app = app_with_deps
    app.state.db_pool = _make_mock_pool(raise_on_acquire=False)

    mock_http = AsyncMock()
    mock_http.get = AsyncMock(return_value=MagicMock(status_code=200))
    app.state.http_client = mock_http

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/health/internal")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    # "service" field is present and non-empty (exact value depends on which
    # app module sys.modules has cached when running cross-service test suites)
    assert "service" in body and body["service"]
    assert body["checks"]["postgres"] == "ok"


@pytest.mark.asyncio
async def test_health_internal_503_has_all_checks(app_with_deps):
    """GET /health/internal → 503 with checks dict when DB is down."""
    app = app_with_deps
    app.state.db_pool = _make_mock_pool(raise_on_acquire=True)

    mock_http = AsyncMock()
    mock_http.get = AsyncMock(return_value=MagicMock(status_code=200))
    app.state.http_client = mock_http

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/health/internal")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert "checks" in body
    assert body["checks"]["postgres"] == "unavailable"


def test_health_check_response_importable():
    """M26 regression: HealthCheckResponse must be exported from jarvis_common."""
    from jarvis_common import HealthCheckResponse

    assert HealthCheckResponse is not None
