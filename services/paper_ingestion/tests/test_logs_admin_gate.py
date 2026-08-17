"""Admin-gate tests for /api/logs/* endpoints.

Verifies that:
- Non-admin browser sessions receive 403 on every /api/logs/* route.
- Admin browser sessions pass through (200 / no 403).
- API-key-only callers (user_role absent from request.state) are allowed
  through — legacy single-tenant compatibility.

These tests exercise the ``require_admin`` dependency by
injecting a fake ``request.state.user_role`` value via a lightweight ASGI
middleware shim, rather than spinning up the full session cookie machinery.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import httpx
import pytest
from httpx import ASGITransport
from jarvis_common.testing import RoleMiddleware

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CID = str(uuid.uuid4())


def _event_row(*, id: int = 1) -> dict:
    return {
        "id": id,
        "created_at": None,
        "level": "info",
        "category": "auth",
        "source": "paper_ingestion",
        "message": "test",
        "context": {},
        "correlation_id": None,
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def _base_app(mock_db):
    """Return (app, pool, conn) with rate-limiter + verify_api_key bypassed."""
    from jarvis_common.auth import verify_api_key
    from jarvis_common.testing_contract_apps import PITestAppOptions, patch_pi_test_app
    from platform_api.deps import get_db_pool, limiter
    from platform_api.main import app

    pool, conn = mock_db
    with patch_pi_test_app(
        pool,
        app=app,
        get_db_pool=get_db_pool,
        limiter=limiter,
        options=PITestAppOptions(
            remove_identity_overrides=False,
            override_db_dependency=True,
            disable_limiter=True,
            dependency_overrides={verify_api_key: lambda: None},
        ),
    ):
        yield app, pool, conn


def _client_with_role(app, role: str | None) -> httpx.AsyncClient:
    """Wrap *app* in the role-injection middleware and return an AsyncClient."""
    wrapped = RoleMiddleware(app, role)
    return httpx.AsyncClient(transport=ASGITransport(app=wrapped), base_url="http://test")


# ---------------------------------------------------------------------------
# 1. Non-admin session → 403 on all routes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_events_non_admin_returns_403(_base_app):
    app, _pool, conn = _base_app
    conn.fetch = AsyncMock(return_value=[])

    async with _client_with_role(app, "user") as client:
        resp = await client.get("/api/logs/events")

    assert resp.status_code == 403, resp.text
    assert "Admin" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_get_event_non_admin_returns_403(_base_app):
    app, _pool, conn = _base_app
    conn.fetchrow = AsyncMock(return_value=None)

    async with _client_with_role(app, "user") as client:
        resp = await client.get("/api/logs/events/1")

    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_summary_non_admin_returns_403(_base_app):
    app, _pool, conn = _base_app
    conn.fetch = AsyncMock(return_value=[])

    async with _client_with_role(app, "user") as client:
        resp = await client.get("/api/logs/summary")

    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_sources_non_admin_returns_403(_base_app):
    app, _pool, conn = _base_app
    conn.fetch = AsyncMock(return_value=[])

    async with _client_with_role(app, "user") as client:
        resp = await client.get("/api/logs/sources")

    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_correlation_non_admin_returns_403(_base_app):
    app, _pool, conn = _base_app
    conn.fetch = AsyncMock(return_value=[])

    async with _client_with_role(app, "user") as client:
        resp = await client.get(f"/api/logs/correlation/{_CID}")

    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# 2. Admin session → 200 on all routes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_events_admin_returns_200(_base_app):
    app, _pool, conn = _base_app
    conn.fetch = AsyncMock(return_value=[_event_row(id=1)])

    async with _client_with_role(app, "admin") as client:
        resp = await client.get("/api/logs/events")

    assert resp.status_code == 200, resp.text
    assert "events" in resp.json()


@pytest.mark.asyncio
async def test_summary_admin_returns_200(_base_app):
    app, _pool, conn = _base_app
    conn.fetch = AsyncMock(return_value=[])

    async with _client_with_role(app, "admin") as client:
        resp = await client.get("/api/logs/summary")

    assert resp.status_code == 200, resp.text
    assert resp.json()["total"] == 0


@pytest.mark.asyncio
async def test_sources_admin_returns_200(_base_app):
    import platform_api.routers.logs as logs_module

    app, _pool, conn = _base_app
    logs_module._sources_cache = None
    conn.fetch = AsyncMock(return_value=[{"source": "paper_ingestion"}])

    async with _client_with_role(app, "admin") as client:
        resp = await client.get("/api/logs/sources")

    assert resp.status_code == 200, resp.text
    assert "paper_ingestion" in resp.json()


# ---------------------------------------------------------------------------
# 3. API-key-only caller (no user_role on request.state) → allowed through
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_events_api_key_only_allowed_through(_base_app):
    """No session role present → legacy API-key path should not be blocked."""
    app, _pool, conn = _base_app
    conn.fetch = AsyncMock(return_value=[])

    # role=None → RoleMiddleware does NOT set request.state.user_role
    async with _client_with_role(app, None) as client:
        resp = await client.get("/api/logs/events")

    # Should pass through (200) — not 403
    assert resp.status_code == 200, resp.text
