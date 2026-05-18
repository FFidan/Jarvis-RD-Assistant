"""Tests for the /health and /health/internal endpoints.

Public /health (ζ4: SEC-H09):
- Returns only {"status": "ok"|"degraded"} — no dependency details exposed.
- HTTP 200 when all deps are reachable; HTTP 503 when any check fails.

Authenticated /health/internal:
- Returns full {status, service, checks} payload.
- Requires valid API key.
"""

from unittest.mock import AsyncMock, MagicMock

# conftest.py has already installed qdrant_client / qdrant_client.models / tiktoken /
# rapidfuzz stubs.
import httpx
import pytest
from httpx import ASGITransport

from tests.conftest import _make_pool_and_conn

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_qdrant_client(*, healthy: bool = True) -> MagicMock:
    client = MagicMock()
    if healthy:
        client.get_collections = AsyncMock(return_value=MagicMock())
    else:
        client.get_collections = AsyncMock(side_effect=ConnectionError("qdrant down"))
    return client


def _make_http_client(
    *,
    litellm_healthy: bool = True,
    ollama_healthy: bool = True,
    vector_healthy: bool = False,  # Vector API is disabled by default → "unknown"
) -> AsyncMock:
    """Return a mock httpx.AsyncClient whose .get() routes per-URL.

    Simulates:
    - LiteLLM /health/readiness → 200 or 503 based on ``litellm_healthy``
    - Ollama /api/tags → 200 or raises ConnectionError based on ``ollama_healthy``
    - Vector /health → 200 or raises ConnectionError based on ``vector_healthy``
    """
    http_client = AsyncMock()

    async def _route_get(url: str, **_kwargs: object) -> MagicMock:
        resp = MagicMock()
        if "health/readiness" in url:
            resp.status_code = 200 if litellm_healthy else 503
        elif "/api/tags" in url:
            if not ollama_healthy:
                raise ConnectionError("ollama down")
            resp.status_code = 200
        elif "vector" in url or "8686" in url:
            if not vector_healthy:
                raise ConnectionError("vector API disabled")
            resp.status_code = 200
        else:
            resp.status_code = 200
        return resp

    http_client.get = AsyncMock(side_effect=_route_get)
    return http_client


# ---------------------------------------------------------------------------
# Fixture: app with mocked state, no auth, no lifespan
# ---------------------------------------------------------------------------


@pytest.fixture()
def _app():
    """Yield an app instance with all dependencies mocked — no lifespan startup."""
    from jarvis_common import verify_api_key
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app

    mock_pool, conn = _make_pool_and_conn()
    # Healthy defaults
    conn.execute = AsyncMock(return_value=None)

    app.state.db_pool = mock_pool
    app.state.qdrant_client = _make_qdrant_client(healthy=True)
    app.state.http_client = _make_http_client(litellm_healthy=True)

    try:
        app.state.limiter.enabled = False
    except AttributeError:
        pass

    app.dependency_overrides[get_db_pool] = lambda: mock_pool
    app.dependency_overrides[verify_api_key] = lambda: None

    yield app, conn
    app.dependency_overrides.clear()
    app.state.limiter.enabled = True


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Public /health tests (status-only, no auth required)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_returns_200_when_all_ok(_app) -> None:
    """GET /health returns HTTP 200 and status='ok' — no dependency details."""
    app, _conn = _app

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
async def test_health_returns_503_when_degraded(_app) -> None:
    """GET /health returns HTTP 503 and status='degraded' when Qdrant is down."""
    app, _conn = _app

    # Override qdrant client to simulate outage
    app.state.qdrant_client = _make_qdrant_client(healthy=False)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/health")

    assert resp.status_code == 503, f"Expected 503 when Qdrant is down, got {resp.status_code}"
    body = resp.json()
    assert body["status"] == "degraded"
    # Public endpoint must NOT expose internal check details
    assert "checks" not in body


@pytest.mark.asyncio
async def test_health_public_no_checks_on_503(_app) -> None:
    """503 from public /health must not include checks dict (SEC-H09)."""
    app, _conn = _app

    app.state.http_client = _make_http_client(litellm_healthy=False)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/health")

    assert resp.status_code == 503
    body = resp.json()
    assert "checks" not in body
    assert body["status"] == "degraded"


# ---------------------------------------------------------------------------
# /health/internal tests (full details, requires auth)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_internal_returns_full_details(_app) -> None:
    """GET /health/internal returns {status, service, checks} when authed.

    Vector is 'unknown' (API disabled by default) which no longer triggers degraded.
    All other checks must be 'ok'.
    """
    app, _conn = _app

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/health/internal")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["service"] == "paper_ingestion"
    checks = body["checks"]
    # Core deps should be ok
    assert checks.get("postgres") == "ok"
    assert checks.get("qdrant") == "ok"
    assert checks.get("litellm") == "ok"
    assert checks.get("ollama") == "ok"
    # Vector API is disabled by default — expected to be 'unknown', not 'unavailable'
    assert checks.get("vector") == "unknown"


@pytest.mark.asyncio
async def test_health_internal_503_has_all_checks(_app) -> None:
    """GET /health/internal returns 503 with full checks when LiteLLM is down."""
    app, _conn = _app

    app.state.http_client = _make_http_client(litellm_healthy=False)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/health/internal")

    assert resp.status_code == 503
    body = resp.json()
    assert "postgres" in body["checks"]
    assert "qdrant" in body["checks"]
    assert "litellm" in body["checks"]
    assert "ollama" in body["checks"]
    assert "vector" in body["checks"]
    assert body["checks"]["litellm"] == "unavailable"


@pytest.mark.asyncio
async def test_health_internal_ollama_down(_app) -> None:
    """GET /health/internal returns 503 when Ollama is unreachable."""
    app, _conn = _app

    app.state.http_client = _make_http_client(ollama_healthy=False)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/health/internal")

    assert resp.status_code == 503
    body = resp.json()
    assert body["checks"]["ollama"] == "unavailable"


@pytest.mark.asyncio
async def test_health_internal_vector_unknown_does_not_degrade(_app) -> None:
    """Vector 'unknown' (API disabled) must not cause overall status=degraded."""
    app, _conn = _app

    # All healthy except vector (which raises by default → unknown)
    app.state.http_client = _make_http_client(
        litellm_healthy=True, ollama_healthy=True, vector_healthy=False
    )

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/health/internal")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["checks"]["vector"] == "unknown"


# ---------------------------------------------------------------------------
# Regression: HEALTH-LIVE-403 + SEC-AUTH-1
#
# The _HEALTH_PATHS exemption in verify_api_key must cover /health/live so
# that unauthenticated liveness probes are never blocked by the global
# app-level dependency. /health/internal must remain 403 without a key.
#
# These tests intentionally do NOT override verify_api_key — they exercise
# the real global dependency gate.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_live_accessible_without_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unauthenticated GET /health/live must return 200 — not 403.

    Exercises the real global verify_api_key dependency (no override) to catch
    regressions where /health/live is accidentally removed from _HEALTH_PATHS.
    """

    import jarvis_common.auth as _auth
    from fastapi import Depends, FastAPI
    from jarvis_common.auth import verify_api_key
    from jarvis_common.health import register_health_routes
    from jarvis_common.settings import get_secrets_settings

    # Set a real-looking API key so verify_api_key enforces key checks and
    # does not fall through to the no-key dev-bypass path.
    test_key = "a" * 32
    monkeypatch.setenv("JARVIS_API_KEY", test_key)
    get_secrets_settings.cache_clear()
    _auth.refresh_api_key_cache()

    # Build a minimal app with the real global dependency — mirrors production
    # wiring in paper_ingestion/main.py which passes dependencies=[Depends(verify_api_key)].
    minimal_app = FastAPI(dependencies=[Depends(verify_api_key)])
    register_health_routes(minimal_app, service_name="test", checks=[])

    async with httpx.AsyncClient(
        transport=ASGITransport(app=minimal_app), base_url="http://test"
    ) as client:
        resp_live = await client.get("/health/live")
        resp_internal = await client.get("/health/internal")

    # /health/live: no auth required — must be 200 (HEALTH-LIVE-403 fix).
    assert resp_live.status_code == 200, (
        f"/health/live returned {resp_live.status_code} without auth — "
        "check that /health/live is in auth._HEALTH_PATHS"
    )
    assert resp_live.json()["status"] == "ok"

    # /health/internal: always requires verify_api_key — must be 403 without key.
    assert resp_internal.status_code == 403, (
        f"/health/internal returned {resp_internal.status_code} without auth — "
        "/health/internal must NOT be exempt from API key auth"
    )
