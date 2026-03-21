"""Tests for settings, nudges, sources, and analytics endpoints.

Covers:
- Config: GET /api/config, GET /api/config/{key}, PUT /api/config/{key}
- Nudges: GET /api/nudges, PUT /api/nudges/{id}
- Sources: GET /api/sources, PUT /api/sources/{id}
- Analytics: GET /api/analytics/papers-by-source, GET /api/analytics/papers-by-status
"""

import sys
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

# Stub heavy native modules unavailable outside Docker.
for _mod_name in ("fitz",):
    if _mod_name not in sys.modules:
        sys.modules[_mod_name] = MagicMock()

import httpx
import pytest
from httpx import ASGITransport


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeRecord(dict):
    """Dict subclass that supports both dict[key] and .keys() like asyncpg.Record."""

    def keys(self):
        return super().keys()


def _make_pool_and_conn():
    """Create a mock asyncpg Pool whose acquire() returns an async CM."""
    conn = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx
    return pool, conn


def _now():
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def _app():
    """Create a minimal app instance with mocked DB pool and disabled auth."""
    from app.main import app, get_db_pool
    from jarvis_common import verify_api_key

    mock_pool, conn = _make_pool_and_conn()
    app.state.db_pool = mock_pool
    app.state.limiter.enabled = False
    mock_http = AsyncMock()
    app.state.http_client = mock_http

    app.dependency_overrides[get_db_pool] = lambda: mock_pool
    app.dependency_overrides[verify_api_key] = lambda: None
    yield app, conn, mock_http
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Tests: Config CRUD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_config(_app):
    """GET /api/config returns list of config entries."""
    app, conn, _ = _app
    conn.fetch.return_value = [
        FakeRecord(key="llm.smart_model", value="mistral-nemo"),
        FakeRecord(key="llm.fast_model", value="qwen3.5:4b"),
    ]

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/config")

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2
    assert body[0]["key"] == "llm.smart_model"


@pytest.mark.asyncio
async def test_get_config_found(_app):
    """GET /api/config/{key} returns the config entry when found."""
    app, conn, _ = _app
    conn.fetchrow.return_value = FakeRecord(key="llm.smart_model", value="mistral-nemo")

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/config/llm.smart_model")

    assert resp.status_code == 200
    body = resp.json()
    assert body["key"] == "llm.smart_model"
    assert body["value"] == "mistral-nemo"


@pytest.mark.asyncio
async def test_get_config_not_found(_app):
    """GET /api/config/{key} returns 404 when key does not exist."""
    app, conn, _ = _app
    conn.fetchrow.return_value = None

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/config/nonexistent.key")

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_set_config_allowed_key(_app):
    """PUT /api/config/{key} sets a config value for an allowed key."""
    app, conn, _ = _app

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.put(
            "/api/config/llm.smart_model",
            json={"key": "llm.smart_model", "value": "new-model"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["key"] == "llm.smart_model"
    assert body["value"] == "new-model"
    conn.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_set_config_disallowed_key(_app):
    """PUT /api/config/{key} returns 400 for a disallowed key."""
    app, conn, _ = _app

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.put(
            "/api/config/secret.password",
            json={"key": "secret.password", "value": "hunter2"},
        )

    assert resp.status_code == 400
    assert "Unknown config key" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Tests: Nudges
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_nudges(_app):
    """GET /api/nudges returns list of scheduled nudges."""
    app, conn, _ = _app
    conn.fetch.return_value = [
        FakeRecord(
            id=1, nudge_type="review_reminder", cron_expression="0 9 * * *",
            enabled=True, config={}, last_fired_at=None, created_at=_now(),
        ),
    ]

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/nudges")

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["nudge_type"] == "review_reminder"


@pytest.mark.asyncio
async def test_update_nudge_found(_app):
    """PUT /api/nudges/{id} updates the nudge when found."""
    app, conn, _ = _app
    existing = FakeRecord(
        id=1, nudge_type="review_reminder", cron_expression="0 9 * * *",
        enabled=True, config={}, last_fired_at=None, created_at=_now(),
    )
    updated = FakeRecord(
        id=1, nudge_type="review_reminder", cron_expression="0 10 * * *",
        enabled=True, config={}, last_fired_at=None, created_at=_now(),
    )
    conn.fetchrow.side_effect = [existing, updated]

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.put(
            "/api/nudges/1",
            json={"cron_expression": "0 10 * * *"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["cron_expression"] == "0 10 * * *"


@pytest.mark.asyncio
async def test_update_nudge_not_found(_app):
    """PUT /api/nudges/{id} returns 404 when nudge does not exist."""
    app, conn, _ = _app
    conn.fetchrow.return_value = None

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.put("/api/nudges/999", json={"enabled": False})

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Tests: Sources
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_sources(_app):
    """GET /api/sources returns list of paper sources."""
    app, conn, _ = _app
    conn.fetch.return_value = [
        FakeRecord(
            id=1, source_type="arxiv", enabled=True, config={},
            priority=1, created_at=_now(),
        ),
    ]

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/sources")

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["source_type"] == "arxiv"


@pytest.mark.asyncio
async def test_update_source_found(_app):
    """PUT /api/sources/{id} updates the source when found."""
    app, conn, _ = _app
    existing = FakeRecord(
        id=1, source_type="arxiv", enabled=True, config={},
        priority=1, created_at=_now(),
    )
    updated = FakeRecord(
        id=1, source_type="arxiv", enabled=False, config={},
        priority=1, created_at=_now(),
    )
    conn.fetchrow.side_effect = [existing, updated]

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.put("/api/sources/1", json={"enabled": False})

    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is False


@pytest.mark.asyncio
async def test_update_source_not_found(_app):
    """PUT /api/sources/{id} returns 404 when source does not exist."""
    app, conn, _ = _app
    conn.fetchrow.return_value = None

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.put("/api/sources/999", json={"enabled": False})

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Tests: Analytics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_papers_by_source(_app):
    """GET /api/analytics/papers-by-source returns paper counts by source."""
    app, conn, _ = _app
    conn.fetch.return_value = [
        FakeRecord(source_type="arxiv", count=25),
        FakeRecord(source_type="semantic_scholar", count=10),
    ]

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/analytics/papers-by-source")

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2
    assert body[0]["source_type"] == "arxiv"
    assert body[0]["count"] == 25


@pytest.mark.asyncio
async def test_papers_by_status(_app):
    """GET /api/analytics/papers-by-status returns paper counts by status."""
    app, conn, _ = _app
    conn.fetch.return_value = [
        FakeRecord(status="new", count=30),
        FakeRecord(status="read", count=15),
    ]

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/analytics/papers-by-status")

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2
    assert body[0]["status"] == "new"
    assert body[0]["count"] == 30
