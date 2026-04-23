"""Tests for PI-006: zotero.* config keys are allowlisted and validated.

Covers:
- Each zotero.* key accepts a valid value (round-trip save returns 200 + correct body)
- zotero.library_type rejects "invalid" with 400
- zotero.poll_cron rejects non-cron string with 400
- zotero.poll_enabled / zotero.auto_push_on_star accept True/False, reject strings
"""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from httpx import ASGITransport

# ---------------------------------------------------------------------------
# Helpers (mirrors test_settings.py style)
# ---------------------------------------------------------------------------


def _make_pool_and_conn():
    """Create a mock asyncpg Pool whose acquire() returns an async CM."""
    conn = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx
    txn_ctx = MagicMock()
    txn_ctx.__aenter__ = AsyncMock(return_value=None)
    txn_ctx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn_ctx)
    return pool, conn


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def _app():
    from jarvis_common import verify_api_key
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app

    mock_pool, conn = _make_pool_and_conn()
    app.state.db_pool = mock_pool
    app.state.limiter.enabled = False
    mock_http = AsyncMock()
    app.state.http_client = mock_http

    app.dependency_overrides[get_db_pool] = lambda: mock_pool
    app.dependency_overrides[verify_api_key] = lambda: None
    yield app, conn
    app.dependency_overrides.clear()
    app.state.limiter.enabled = True


# ---------------------------------------------------------------------------
# Helper: perform a PUT /api/config/{key} request
# ---------------------------------------------------------------------------


async def _put_config(app, key: str, value):
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.put(f"/api/config/{key}", json={"key": key, "value": value})


# ---------------------------------------------------------------------------
# Tests: valid values accepted (round-trip)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_zotero_api_key_valid(_app):
    """zotero.api_key accepts a non-empty string."""
    app, conn = _app
    resp = await _put_config(app, "zotero.api_key", "abc123")
    assert resp.status_code == 200
    body = resp.json()
    assert body["key"] == "zotero.api_key"
    assert body["value"] == "abc123"
    conn.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_zotero_user_id_valid(_app):
    """zotero.user_id accepts a non-empty string (numeric ID as string)."""
    app, conn = _app
    resp = await _put_config(app, "zotero.user_id", "12345678")
    assert resp.status_code == 200
    body = resp.json()
    assert body["key"] == "zotero.user_id"
    assert body["value"] == "12345678"
    conn.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_zotero_library_type_user(_app):
    """zotero.library_type accepts 'user'."""
    app, conn = _app
    resp = await _put_config(app, "zotero.library_type", "user")
    assert resp.status_code == 200
    assert resp.json()["value"] == "user"


@pytest.mark.asyncio
async def test_zotero_library_type_group(_app):
    """zotero.library_type accepts 'group'."""
    app, conn = _app
    resp = await _put_config(app, "zotero.library_type", "group")
    assert resp.status_code == 200
    assert resp.json()["value"] == "group"


@pytest.mark.asyncio
async def test_zotero_poll_enabled_true(_app):
    """zotero.poll_enabled accepts True."""
    app, conn = _app
    resp = await _put_config(app, "zotero.poll_enabled", True)
    assert resp.status_code == 200
    assert resp.json()["value"] is True


@pytest.mark.asyncio
async def test_zotero_poll_enabled_false(_app):
    """zotero.poll_enabled accepts False."""
    app, conn = _app
    resp = await _put_config(app, "zotero.poll_enabled", False)
    assert resp.status_code == 200
    assert resp.json()["value"] is False


@pytest.mark.asyncio
async def test_zotero_poll_cron_valid(_app):
    """zotero.poll_cron accepts a valid cron expression."""
    app, conn = _app
    resp = await _put_config(app, "zotero.poll_cron", "0 * * * *")
    assert resp.status_code == 200
    assert resp.json()["value"] == "0 * * * *"


@pytest.mark.asyncio
async def test_zotero_auto_push_on_star_true(_app):
    """zotero.auto_push_on_star accepts True."""
    app, conn = _app
    resp = await _put_config(app, "zotero.auto_push_on_star", True)
    assert resp.status_code == 200
    assert resp.json()["value"] is True


@pytest.mark.asyncio
async def test_zotero_auto_push_on_star_false(_app):
    """zotero.auto_push_on_star accepts False."""
    app, conn = _app
    resp = await _put_config(app, "zotero.auto_push_on_star", False)
    assert resp.status_code == 200
    assert resp.json()["value"] is False


# ---------------------------------------------------------------------------
# Tests: invalid values rejected with 400
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_zotero_library_type_invalid(_app):
    """zotero.library_type rejects values other than 'user' or 'group'."""
    app, conn = _app
    resp = await _put_config(app, "zotero.library_type", "invalid")
    assert resp.status_code == 400
    assert "user" in resp.json()["detail"] or "group" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_zotero_poll_cron_invalid(_app):
    """zotero.poll_cron rejects non-cron strings."""
    app, conn = _app
    resp = await _put_config(app, "zotero.poll_cron", "not a cron")
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "invalid cron" in detail.lower() or "cron" in detail.lower()


@pytest.mark.asyncio
async def test_zotero_poll_enabled_rejects_string(_app):
    """zotero.poll_enabled rejects string values."""
    app, conn = _app
    resp = await _put_config(app, "zotero.poll_enabled", "true")
    assert resp.status_code == 400
    assert "boolean" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_zotero_auto_push_on_star_rejects_string(_app):
    """zotero.auto_push_on_star rejects string values."""
    app, conn = _app
    resp = await _put_config(app, "zotero.auto_push_on_star", "yes")
    assert resp.status_code == 400
    assert "boolean" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_zotero_api_key_rejects_empty_string(_app):
    """zotero.api_key rejects empty string."""
    app, conn = _app
    resp = await _put_config(app, "zotero.api_key", "")
    assert resp.status_code == 400
    assert "non-empty" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_zotero_user_id_rejects_empty_string(_app):
    """zotero.user_id rejects empty string."""
    app, conn = _app
    resp = await _put_config(app, "zotero.user_id", "")
    assert resp.status_code == 400
    assert "non-empty" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_zotero_key_not_blocked_by_allowlist(_app):
    """All 6 zotero.* keys are in the allowlist (no 'Unknown config key' rejection)."""
    app, conn = _app
    zotero_keys = [
        ("zotero.api_key", "key123"),
        ("zotero.user_id", "99999"),
        ("zotero.library_type", "user"),
        ("zotero.poll_enabled", True),
        ("zotero.poll_cron", "0 4 * * *"),
        ("zotero.auto_push_on_star", False),
    ]
    for key, value in zotero_keys:
        resp = await _put_config(app, key, value)
        assert resp.status_code == 200, f"Key {key!r} was blocked by allowlist: {resp.json()}"
        conn.execute.reset_mock()
