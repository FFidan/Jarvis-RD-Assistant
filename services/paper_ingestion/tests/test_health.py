"""Tests for the /health and /health/internal endpoints.

Public /health (ζ4: SEC-H09):
- Returns only {"status": "ok"|"degraded"} — no dependency details exposed.
- HTTP 200 when all deps are reachable; HTTP 503 when any check fails.

Authenticated /health/internal:
- Returns full {status, service, checks} payload.
- Requires valid API key.
"""

import sys
import types
from unittest.mock import AsyncMock, MagicMock

# ---------------------------------------------------------------------------
# Stubs for Docker-only dependencies (must precede any app.* import)
# ---------------------------------------------------------------------------
if "qdrant_client" not in sys.modules:
    _fake_qdrant = types.ModuleType("qdrant_client")
    setattr(_fake_qdrant, "AsyncQdrantClient", MagicMock())
    sys.modules["qdrant_client"] = _fake_qdrant

if "qdrant_client.models" not in sys.modules:
    _fake_qm = types.ModuleType("qdrant_client.models")
    for _attr in ("Distance", "PointIdsList", "PointStruct", "VectorParams"):
        setattr(_fake_qm, _attr, MagicMock())
    sys.modules["qdrant_client.models"] = _fake_qm

if "tiktoken" not in sys.modules:
    _fake_tiktoken = types.ModuleType("tiktoken")
    setattr(_fake_tiktoken, "get_encoding", MagicMock(return_value=MagicMock()))
    sys.modules["tiktoken"] = _fake_tiktoken

if "rapidfuzz" not in sys.modules:
    _fake_rapidfuzz = types.ModuleType("rapidfuzz")
    setattr(_fake_rapidfuzz, "fuzz", MagicMock())
    sys.modules["rapidfuzz"] = _fake_rapidfuzz

import httpx
import pytest
from httpx import ASGITransport

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pool_and_conn():
    """Return (pool, conn) with async-context-manager support."""
    conn = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx
    return pool, conn


def _make_qdrant_client(*, healthy: bool = True) -> MagicMock:
    client = MagicMock()
    if healthy:
        client.get_collections = AsyncMock(return_value=MagicMock())
    else:
        client.get_collections = AsyncMock(side_effect=ConnectionError("qdrant down"))
    return client


def _make_http_client(*, litellm_healthy: bool = True) -> AsyncMock:
    """Return a mock httpx.AsyncClient whose .get() simulates LiteLLM /health/readiness."""
    http_client = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200 if litellm_healthy else 503
    http_client.get = AsyncMock(return_value=mock_resp)
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
    """GET /health/internal returns {status, service, checks} when authed."""
    app, _conn = _app

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/health/internal")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["service"] == "paper_ingestion"
    assert all(v == "ok" for v in body["checks"].values())


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
    assert body["checks"]["litellm"] == "unavailable"
