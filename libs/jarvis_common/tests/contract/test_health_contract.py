"""Shared health contract suite (audit X-02).

Replaces the triplicated per-service health tests (~6-9 tests). One
parametrized suite over the 3 health surfaces asserting the shared
behavior: 200 + status field present + auth-not-required for public
routes.

Route contracts (from jarvis_common.health.register_health_routes and
telegram_bot.internal_api):

  GET /health/live  — no auth, no probes, always {"status": "ok"}, 200
  GET /health       — no auth, {"status": "ok"|"degraded"} only (SEC-H09),
                      200 ok / 503 degraded
  GET /health/internal — requires verify_api_key, full HealthCheckResponse
                         {status, service, checks}, same 200/503 split

telegram_bot exposes GET /health on _internal_app (not register_health_routes).
It returns {"status": "ok"} unconditionally with no auth.  It is included in
the no-auth / status-present assertions only; the degraded and /health/internal
cases apply only to services using register_health_routes.

Per-service test_health_*.py files in Sub-wave 4.4 collapse to a thin
"is the route wired" smoke (one test per service), citing this suite as
their survivor.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from httpx import ASGITransport

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_pool(*, raise_on_acquire: bool = False) -> MagicMock:
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


def _make_mock_http(*, healthy: bool = True) -> AsyncMock:
    """Mock httpx.AsyncClient whose .get() returns 200 (healthy) or raises (unhealthy)."""
    client = AsyncMock()
    if healthy:
        resp = MagicMock()
        resp.status_code = 200
        client.get = AsyncMock(return_value=resp)
    else:
        client.get = AsyncMock(side_effect=ConnectionError("dependency down"))
    return client


def _wire_pi_app(*, db_up: bool = True, http_healthy: bool = True) -> Any:
    """Return paper_ingestion.app with all state mocked; clears overrides on test exit."""
    from jarvis_common import verify_api_key
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app

    pool = _make_mock_pool(raise_on_acquire=not db_up)
    http = _make_mock_http(healthy=http_healthy)
    qdrant = MagicMock()
    qdrant.get_collections = AsyncMock(return_value=MagicMock())

    app.state.db_pool = pool
    app.state.http_client = http
    app.state.qdrant_client = qdrant
    try:
        app.state.limiter.enabled = False
    except AttributeError:
        pass

    app.dependency_overrides[get_db_pool] = lambda: pool
    app.dependency_overrides[verify_api_key] = lambda: None
    return app


def _wire_le_app(*, db_up: bool = True, http_healthy: bool = True) -> Any:
    """Return learning_engine.app with all state mocked."""
    from jarvis_common import verify_api_key
    from learning_engine.deps import get_db_pool
    from learning_engine.main import app

    pool = _make_mock_pool(raise_on_acquire=not db_up)
    http = _make_mock_http(healthy=http_healthy)

    app.state.db_pool = pool
    app.state.http_client = http
    try:
        app.state.limiter.enabled = False
    except AttributeError:
        pass

    app.dependency_overrides[get_db_pool] = lambda: pool
    app.dependency_overrides[verify_api_key] = lambda: None
    return app


# ---------------------------------------------------------------------------
# Parametrised app fixture — covers the two register_health_routes services.
# telegram_bot._internal_app is handled separately below because it does not
# use register_health_routes and has no degraded path or /health/internal.
# ---------------------------------------------------------------------------


@pytest.fixture(
    params=[
        "paper_ingestion",
        "learning_engine",
    ]
)
def rhr_app(request: pytest.FixtureRequest):
    """Yield (service_name, wired_app) for services using register_health_routes."""
    name = request.param
    if name == "paper_ingestion":
        app = _wire_pi_app()
    else:
        app = _wire_le_app()
    yield name, app
    app.dependency_overrides.clear()
    try:
        app.state.limiter.enabled = True
    except AttributeError:
        pass


# ---------------------------------------------------------------------------
# Contract: GET /health — public, status-only
# ---------------------------------------------------------------------------


async def test_health_public_200_when_ok(rhr_app):
    """GET /health → 200 with status='ok' when all probes pass."""
    _name, app = rhr_app
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"


async def test_health_public_exposes_only_status(rhr_app):
    """GET /health must NOT expose service or checks keys (SEC-H09)."""
    _name, app = rhr_app
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/health")
    body = resp.json()
    assert "service" not in body
    assert "checks" not in body


async def test_health_public_503_when_db_down(rhr_app):
    """GET /health → 503 with status='degraded' and no checks when DB fails."""
    name, _app = rhr_app
    # Build a fresh wired app with DB down; avoid mutating the fixture's app
    if name == "paper_ingestion":
        app = _wire_pi_app(db_up=False)
    else:
        app = _wire_le_app(db_up=False)
    try:
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/health")
        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "degraded"
        assert "checks" not in body
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Contract: GET /health/live — always 200, no auth, no probes
# ---------------------------------------------------------------------------


async def test_health_live_always_200(rhr_app):
    """GET /health/live → 200 {"status": "ok"} unconditionally."""
    _name, app = rhr_app
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/health/live")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_health_live_no_auth_required(rhr_app, monkeypatch: pytest.MonkeyPatch):
    """GET /health/live returns 200 even without an API key (HEALTH-LIVE-403 regression guard).

    Builds a minimal app with real verify_api_key enforced so the _HEALTH_PATHS
    exemption is exercised rather than bypassed by the fixture's dependency override.
    """
    import jarvis_common.auth as _auth
    from fastapi import Depends, FastAPI
    from jarvis_common.auth import verify_api_key
    from jarvis_common.health import register_health_routes
    from jarvis_common.settings import get_secrets_settings

    test_key = "a" * 32
    monkeypatch.setenv("JARVIS_API_KEY", test_key)
    get_secrets_settings.cache_clear()
    _auth.refresh_api_key_cache()

    minimal_app = FastAPI(dependencies=[Depends(verify_api_key)])
    register_health_routes(minimal_app, service_name="test-live", checks=[])

    async with httpx.AsyncClient(
        transport=ASGITransport(app=minimal_app), base_url="http://test"
    ) as c:
        resp_live = await c.get("/health/live")
        resp_internal = await c.get("/health/internal")

    assert resp_live.status_code == 200, (
        f"/health/live returned {resp_live.status_code} without auth — "
        "check that /health/live is in auth._HEALTH_PATHS"
    )
    assert resp_live.json()["status"] == "ok"
    # /health/internal is NOT in _HEALTH_PATHS — must 403 without key
    assert resp_internal.status_code == 403, (
        f"/health/internal returned {resp_internal.status_code} without auth — "
        "/health/internal must NOT be exempt from API key auth"
    )


# ---------------------------------------------------------------------------
# Contract: GET /health/internal — authenticated, full HealthCheckResponse
# ---------------------------------------------------------------------------


async def test_health_internal_200_full_payload(rhr_app):
    """GET /health/internal → 200 with {status, service, checks} when all probes pass."""
    name, app = rhr_app
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/health/internal")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    # service matches the service_name passed to register_health_routes
    assert body["service"] == name
    assert "checks" in body
    assert isinstance(body["checks"], dict)
    # postgres probe is registered for both services
    assert body["checks"].get("postgres") == "ok"


async def test_health_internal_503_when_db_down(rhr_app):
    """GET /health/internal → 503 with degraded status and checks dict when DB fails."""
    name, _app = rhr_app
    if name == "paper_ingestion":
        app = _wire_pi_app(db_up=False)
    else:
        app = _wire_le_app(db_up=False)
    try:
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/health/internal")
        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "degraded"
        assert "checks" in body
        assert body["checks"]["postgres"] == "unavailable"
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# telegram_bot._internal_app: minimal health surface
#
# The bot exposes GET /health on _internal_app (not via register_health_routes).
# It returns {"status": "ok"} unconditionally with no auth required.
# There is no degraded path and no /health/internal route.
# ---------------------------------------------------------------------------


@pytest.fixture()
def tg_internal_app():
    from telegram_bot.internal_api import _internal_app

    return _internal_app


async def test_telegram_bot_health_200(tg_internal_app):
    """GET /health on telegram_bot._internal_app → 200 {"status": "ok"}."""
    async with httpx.AsyncClient(
        transport=ASGITransport(app=tg_internal_app), base_url="http://test"
    ) as c:
        resp = await c.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_telegram_bot_health_no_auth_required(tg_internal_app):
    """GET /health on telegram_bot._internal_app requires no API key."""
    # Call without any X-API-Key header — must still succeed
    async with httpx.AsyncClient(
        transport=ASGITransport(app=tg_internal_app), base_url="http://test"
    ) as c:
        resp = await c.get("/health", headers={})
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
