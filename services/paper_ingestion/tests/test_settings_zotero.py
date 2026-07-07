"""Tests for zotero.* config keys being allowlisted and validated.

Covers:
- Each zotero.* key accepts a valid value (round-trip save returns 200 + correct body)
- zotero.library_type rejects "invalid" with 400
- zotero.poll_cron rejects non-cron string with 400
- zotero.poll_enabled / zotero.auto_push_on_star accept True/False, reject strings
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from cryptography.fernet import Fernet
from httpx import ASGITransport
from jarvis_common.crypto import refresh_fernet_cache

from tests.conftest import _make_pool_and_conn

# ---------------------------------------------------------------------------
# Helpers (mirrors test_settings.py style)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def fernet_key(monkeypatch):
    """Generate a fresh Fernet key and wire it into JARVIS_CONFIG_KEY for the test."""
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("JARVIS_CONFIG_KEY", key)
    refresh_fernet_cache()
    yield key
    refresh_fernet_cache()


@pytest.fixture()
def _app():
    from jarvis_common import verify_api_key
    from jarvis_common.auth import current_user_id_strict
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app

    mock_pool, conn = _make_pool_and_conn()
    app.state.db_pool = mock_pool
    app.state.limiter.enabled = False
    mock_http = AsyncMock()
    app.state.http_client = mock_http

    app.dependency_overrides[get_db_pool] = lambda: mock_pool
    app.dependency_overrides[verify_api_key] = lambda: None
    # Settings routes now hard-401 sessionless callers.
    # Inject a concrete authenticated user for the duration of the test.
    # current_user_id_strict is resolved via Depends, so steer it through
    # app.dependency_overrides (a module-symbol swap no longer reaches the route).
    app.dependency_overrides[current_user_id_strict] = lambda: 1
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
@pytest.mark.usefixtures("fernet_key")
async def test_zotero_api_key_valid(_app):
    """zotero.api_key accepts a non-empty string; response is masked (encrypted key)."""
    app, conn = _app
    resp = await _put_config(app, "zotero.api_key", "abc123")
    assert resp.status_code == 200
    body = resp.json()
    assert body["key"] == "zotero.api_key"
    # zotero.api_key is now an encrypted key — response returns masked preview
    # H.1: mask_secret now returns "****" + last 4 chars (was prefix + "****")
    assert body["value"] == "****c123"
    # set_config emits a log_event after the UPSERT — expect at least one execute call.
    conn.execute.assert_awaited()


@pytest.mark.asyncio
async def test_zotero_user_id_valid(_app):
    """zotero.user_id accepts a non-empty string (numeric ID as string)."""
    app, conn = _app
    resp = await _put_config(app, "zotero.user_id", "12345678")
    assert resp.status_code == 200
    body = resp.json()
    assert body["key"] == "zotero.user_id"
    assert body["value"] == "12345678"
    # set_config emits a log_event after the UPSERT — expect at least one execute call.
    conn.execute.assert_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("library_type", ["user", "group"])
async def test_zotero_library_type_valid(_app, library_type: str):
    """zotero.library_type accepts 'user' and 'group' (D5-10)."""
    app, _conn = _app
    resp = await _put_config(app, "zotero.library_type", library_type)
    assert resp.status_code == 200
    assert resp.json()["value"] == library_type


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("key", "value"),
    [
        pytest.param("zotero.poll_enabled", True, id="poll_enabled_true"),
        pytest.param("zotero.poll_enabled", False, id="poll_enabled_false"),
        pytest.param("zotero.auto_push_on_star", True, id="auto_push_on_star_true"),
        pytest.param("zotero.auto_push_on_star", False, id="auto_push_on_star_false"),
    ],
)
async def test_zotero_bool_key_accepts_bool(_app, key: str, value: bool):
    """Boolean zotero keys accept True and False; round-trip value is preserved (D5-07)."""
    app, _conn = _app
    resp = await _put_config(app, key, value)
    assert resp.status_code == 200
    assert resp.json()["value"] is value


@pytest.mark.asyncio
async def test_zotero_poll_enabled_write_reconciles_caller_job(_app):
    """Personal Zotero readiness writes should reconcile only the caller's poll job."""
    app, _conn = _app
    app.state.scheduler = MagicMock()

    with patch("paper_ingestion.scheduler.reconcile_zotero_poll_job", new=AsyncMock()) as reconcile:
        resp = await _put_config(app, "zotero.poll_enabled", True)

    assert resp.status_code == 200
    reconcile.assert_awaited_once()
    assert reconcile.await_args.kwargs["app"] is app
    assert reconcile.await_args.kwargs["user_id"] == 1


@pytest.mark.asyncio
async def test_zotero_poll_cron_valid(_app):
    """zotero.poll_cron accepts a valid cron expression."""
    app, _conn = _app
    resp = await _put_config(app, "zotero.poll_cron", "0 * * * *")
    assert resp.status_code == 200
    assert resp.json()["value"] == "0 * * * *"


# ---------------------------------------------------------------------------
# Tests: invalid values rejected with 400
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_zotero_library_type_invalid(_app):
    """zotero.library_type rejects values other than 'user' or 'group'."""
    app, _conn = _app
    resp = await _put_config(app, "zotero.library_type", "invalid")
    assert resp.status_code == 400
    assert "user" in resp.json()["detail"] or "group" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_zotero_poll_cron_invalid(_app):
    """zotero.poll_cron rejects non-cron strings."""
    app, _conn = _app
    resp = await _put_config(app, "zotero.poll_cron", "not a cron")
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "invalid cron" in detail.lower() or "cron" in detail.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key,bad_value",
    [
        pytest.param("zotero.poll_enabled", "true", id="poll_enabled"),
        pytest.param("zotero.auto_push_on_star", "yes", id="auto_push_on_star"),
    ],
)
async def test_zotero_bool_key_rejects_string(_app, key: str, bad_value: str):
    """Boolean zotero keys reject string values with 400 (D5-10)."""
    app, _conn = _app
    resp = await _put_config(app, key, bad_value)
    assert resp.status_code == 400
    assert "boolean" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_zotero_api_key_rejects_empty_string(_app):
    """zotero.api_key rejects empty string."""
    app, _conn = _app
    resp = await _put_config(app, "zotero.api_key", "")
    assert resp.status_code == 400
    assert "non-empty" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_zotero_user_id_rejects_empty_string(_app):
    """zotero.user_id rejects empty string."""
    app, _conn = _app
    resp = await _put_config(app, "zotero.user_id", "")
    assert resp.status_code == 400
    assert "non-empty" in resp.json()["detail"]


async def _get_config(app, key: str):
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.get(f"/api/config/{key}")


# ---------------------------------------------------------------------------
# Tests: GET /api/config/{key} secret masking
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_zotero_api_key_returns_masked(_app):
    """GET zotero.api_key returns masked preview, not the real value.

    zotero.api_key is in _ENCRYPTED_KEYS. When the row has only a legacy plaintext
    value (encrypted_value = NULL), _resolve_config_value falls back to masking the
    plaintext via mask_secret(), which yields '****' + last-4-chars (H.1).
    """
    app, conn = _app
    # Simulate a legacy plaintext row (encrypted_value = NULL)
    conn.fetchrow.return_value = {
        "key": "zotero.api_key",
        "value": "supersecret123",
        "encrypted_value": None,
    }
    resp = await _get_config(app, "zotero.api_key")
    assert resp.status_code == 200
    body = resp.json()
    assert body["key"] == "zotero.api_key"
    # mask_secret("supersecret123") → "****t123" (H.1)
    assert body["value"] == "****t123"
    assert "supersecret123" not in resp.text


@pytest.mark.asyncio
async def test_get_zotero_user_id_not_masked(_app):
    """GET zotero.user_id returns the real value (not a secret key)."""
    app, conn = _app
    conn.fetchrow.return_value = {"key": "zotero.user_id", "value": "12345678"}
    resp = await _get_config(app, "zotero.user_id")
    assert resp.status_code == 200
    assert resp.json()["value"] == "12345678"


@pytest.mark.asyncio
async def test_get_unknown_config_key_returns_404(_app):
    """GET /api/config/{key} rejects unknown keys with 404 (allowlist check)."""
    app, conn = _app
    resp = await _get_config(app, "unknown.secret.key")
    assert resp.status_code == 404
    # DB should NOT have been queried for unknown keys
    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_zotero_api_key_not_found_returns_404(_app):
    """GET zotero.api_key returns 404 when the key is not set in the DB."""
    app, conn = _app
    conn.fetchrow.return_value = None
    resp = await _get_config(app, "zotero.api_key")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_zotero_key_not_blocked_by_allowlist(_app, fernet_key):
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
