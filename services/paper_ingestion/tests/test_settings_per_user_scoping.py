"""Tests for WS-2C per-user settings scoping.

Covers:
- System keys require admin role (403 for non-admin browser session).
- Personal keys are writable by any authenticated caller.
- No role on request.state (API-key-only callers) passes through on system keys.
- Classification map sanity: every allowed key is classified personal or system.
- Fallback-to-system-default: GET returns system row value when no per-user row.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import HTTPException
from httpx import ASGITransport

from tests.conftest import FakeRecord, _make_pool_and_conn

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app():
    """Build a test app with auth and rate-limiter bypassed."""
    from jarvis_common import verify_api_key
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app

    pool, conn = _make_pool_and_conn()
    app.state.db_pool = pool
    app.state.limiter.enabled = False
    mock_http = AsyncMock()
    app.state.http_client = mock_http

    app.dependency_overrides[get_db_pool] = lambda: pool
    app.dependency_overrides[verify_api_key] = lambda: None

    return app, pool, conn, mock_http


# Coroutines for monkeypatching require_admin in the settings module namespace.
# require_admin is called directly (not via Depends) inside set_config, so
# dependency_overrides cannot intercept it — we must patch the module attribute.


async def _require_admin_deny(_request):
    """Simulates a non-admin browser session: always raises 403."""
    raise HTTPException(status_code=403, detail="Admin role required")


async def _require_admin_allow(_request):
    """Simulates an admin or API-key-only caller: always passes."""
    return None


# ---------------------------------------------------------------------------
# Tests: classification map
# ---------------------------------------------------------------------------


def test_all_allowed_keys_classified():
    """Every key in _ALLOWED_CONFIG_KEYS is classified personal or system (not unknown)."""
    from paper_ingestion.routers.settings import (
        _ALLOWED_CONFIG_KEYS,
        _classify_config_key,
    )

    unclassified = [key for key in _ALLOWED_CONFIG_KEYS if _classify_config_key(key) == "unknown"]
    assert not unclassified, (
        f"Keys in _ALLOWED_CONFIG_KEYS that lack a classification: {unclassified}"
    )


def test_personal_and_system_sets_are_disjoint():
    """PERSONAL_KEYS and SYSTEM_KEYS must not overlap."""
    from paper_ingestion.routers.settings import PERSONAL_KEYS, SYSTEM_KEYS

    overlap = PERSONAL_KEYS & SYSTEM_KEYS
    assert not overlap, f"Keys appear in both PERSONAL_KEYS and SYSTEM_KEYS: {overlap}"


def test_personal_keys_classify_as_personal():
    """Every key in PERSONAL_KEYS classifies as 'personal'."""
    from paper_ingestion.routers.settings import PERSONAL_KEYS, _classify_config_key

    for key in PERSONAL_KEYS:
        assert _classify_config_key(key) == "personal", f"{key!r} should be 'personal'"


def test_system_keys_classify_as_system():
    """Every key in SYSTEM_KEYS classifies as 'system'."""
    from paper_ingestion.routers.settings import SYSTEM_KEYS, _classify_config_key

    for key in SYSTEM_KEYS:
        assert _classify_config_key(key) == "system", f"{key!r} should be 'system'"


def test_dynamic_num_ctx_classifies_as_system():
    """Dynamic llm.<hostname>.<role>_num_ctx keys classify as system."""
    from paper_ingestion.routers.settings import _classify_config_key

    assert _classify_config_key("llm.host-rtx5060.smart_num_ctx") == "system"
    assert _classify_config_key("llm.myhost.embed_num_ctx") == "system"


def test_dynamic_thinking_disabled_classifies_as_system():
    """Dynamic llm.<hostname>.thinking_disabled.<model_id> keys classify as system."""
    from paper_ingestion.routers.settings import _classify_config_key

    assert _classify_config_key("llm.myhost.thinking_disabled.qwen3:14b") == "system"


# ---------------------------------------------------------------------------
# Tests: system keys require admin role
#
# require_admin is called directly inside set_config (not via Depends), so we
# monkeypatch the function in the settings module namespace with patch().
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_system_key_pulse_blocked_for_non_admin_session():
    """PUT /api/config/pulse.enabled returns 403 for a non-admin browser session."""
    app, pool, conn, _ = _make_app()
    try:
        with patch(
            "paper_ingestion.routers.settings.require_admin",
            new=_require_admin_deny,
        ):
            async with httpx.AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.put(
                    "/api/config/pulse.enabled",
                    json={"key": "pulse.enabled", "value": True},
                )
        assert resp.status_code == 403, (
            f"Expected 403 for non-admin writing system key, got {resp.status_code}: {resp.json()}"
        )
        assert "Admin role required" in resp.json()["detail"]
    finally:
        app.dependency_overrides.clear()
        app.state.limiter.enabled = True


@pytest.mark.asyncio
async def test_system_key_llm_model_blocked_for_non_admin_session():
    """PUT /api/config/llm.smart_model returns 403 for a non-admin browser session.

    The admin check runs before model validation, so the 403 is emitted even
    when the model would otherwise be valid.
    """
    app, pool, conn, mock_http = _make_app()
    mock_http.get.return_value = MagicMock(
        status_code=200,
        json=MagicMock(return_value={"models": [{"name": "qwen3:4b"}]}),
    )
    try:
        with patch(
            "paper_ingestion.routers.settings.require_admin",
            new=_require_admin_deny,
        ):
            async with httpx.AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.put(
                    "/api/config/llm.smart_model",
                    json={"key": "llm.smart_model", "value": "qwen3:4b"},
                )
        assert resp.status_code == 403, (
            f"Expected 403 for non-admin writing system key, got {resp.status_code}: {resp.json()}"
        )
    finally:
        app.dependency_overrides.clear()
        app.state.limiter.enabled = True


@pytest.mark.asyncio
async def test_system_key_allowed_for_admin_session():
    """PUT /api/config/pulse.enabled succeeds (200) for an admin browser session."""
    app, pool, conn, _ = _make_app()
    try:
        with patch(
            "paper_ingestion.routers.settings.require_admin",
            new=_require_admin_allow,
        ):
            async with httpx.AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.put(
                    "/api/config/pulse.enabled",
                    json={"key": "pulse.enabled", "value": True},
                )
        assert resp.status_code == 200, (
            f"Expected 200 for admin writing system key, got {resp.status_code}: {resp.json()}"
        )
    finally:
        app.dependency_overrides.clear()
        app.state.limiter.enabled = True


@pytest.mark.asyncio
async def test_system_key_allowed_for_api_key_only_caller():
    """System key write passes for an API-key-only caller (no browser session).

    API-key-only callers (Telegram bot, cron, DEV_MODE single-tenant) have no
    user_role on request.state, so require_admin allows them through.  This
    test verifies the allow-path produces a non-403 response.
    """
    app, pool, conn, mock_http = _make_app()
    mock_http.get.return_value = MagicMock(
        status_code=200,
        json=MagicMock(return_value={"models": [{"name": "qwen3:4b"}]}),
    )
    try:
        with patch(
            "paper_ingestion.routers.settings.require_admin",
            new=_require_admin_allow,
        ):
            async with httpx.AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.put(
                    "/api/config/llm.fast_model",
                    json={"key": "llm.fast_model", "value": "qwen3:4b"},
                )
        # Must not be 403 regardless of model validation result
        assert resp.status_code != 403, f"API-key-only caller was rejected with 403: {resp.json()}"
    finally:
        app.dependency_overrides.clear()
        app.state.limiter.enabled = True


# ---------------------------------------------------------------------------
# Tests: personal keys accessible to any caller
#
# These tests verify that require_admin is NOT called for personal keys by
# patching it to always-deny — if the key were classified as system, the
# response would be 403.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_personal_key_timezone_accessible_to_non_admin():
    """PUT /api/config/user.timezone succeeds regardless of admin status.

    Verifies that personal keys bypass the require_admin gate entirely.
    """
    app, pool, conn, _ = _make_app()
    try:
        with patch(
            "paper_ingestion.routers.settings.require_admin",
            new=_require_admin_deny,
        ):
            async with httpx.AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.put(
                    "/api/config/user.timezone",
                    json={"key": "user.timezone", "value": "Europe/Berlin"},
                )
        assert resp.status_code == 200, (
            f"Personal key user.timezone should be writable by any user, "
            f"got {resp.status_code}: {resp.json()}"
        )
    finally:
        app.dependency_overrides.clear()
        app.state.limiter.enabled = True


@pytest.mark.asyncio
async def test_personal_key_fsrs_accessible_to_non_admin():
    """PUT /api/config/fsrs.desired_retention succeeds regardless of admin status."""
    app, pool, conn, _ = _make_app()
    try:
        with patch(
            "paper_ingestion.routers.settings.require_admin",
            new=_require_admin_deny,
        ):
            async with httpx.AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.put(
                    "/api/config/fsrs.desired_retention",
                    json={"key": "fsrs.desired_retention", "value": 0.85},
                )
        assert resp.status_code == 200, (
            f"Personal key fsrs.desired_retention should be writable by any user, "
            f"got {resp.status_code}: {resp.json()}"
        )
    finally:
        app.dependency_overrides.clear()
        app.state.limiter.enabled = True


@pytest.mark.asyncio
async def test_personal_zotero_api_key_accessible_to_non_admin(monkeypatch):
    """PUT /api/config/zotero.api_key succeeds regardless of admin status.

    zotero.api_key is encrypted; monkeypatch encrypt_secret to avoid FERNET_KEY dep.
    """
    monkeypatch.setattr(
        "paper_ingestion.routers.settings.encrypt_secret",
        lambda v: "encrypted-value",
    )
    app, pool, conn, _ = _make_app()
    try:
        with patch(
            "paper_ingestion.routers.settings.require_admin",
            new=_require_admin_deny,
        ):
            async with httpx.AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.put(
                    "/api/config/zotero.api_key",
                    json={"key": "zotero.api_key", "value": "my-zotero-api-key"},
                )
        assert resp.status_code == 200, (
            f"Personal Zotero key should be writable by any user, "
            f"got {resp.status_code}: {resp.json()}"
        )
    finally:
        app.dependency_overrides.clear()
        app.state.limiter.enabled = True


@pytest.mark.asyncio
async def test_zotero_library_scope_change_resets_user_cursor(monkeypatch):
    """Changing Zotero library identity must force the next poll to read the new scope."""
    from paper_ingestion.routers import settings

    pool, conn = _make_pool_and_conn()
    monkeypatch.setattr(settings, "current_user_id_strict", AsyncMock(return_value=42))

    await settings.set_config.__wrapped__(
        MagicMock(),
        key="zotero.library_type",
        body=settings.ConfigEntry(key="zotero.library_type", value="group"),
        db_pool=pool,
        scheduler=None,
    )

    assert any(
        "zotero.last_library_version" in str(call.args[0]) and call.args[1] == 42
        for call in conn.execute.await_args_list
    )


# ---------------------------------------------------------------------------
# Tests: fallback-to-system-default (GET read)
#
# Today user_config has no user_id column, so every read returns the single
# system row. This test documents the current behaviour: GET /api/config/{key}
# returns the system row value (the effective default for all users).
# In Wave-3 this will be replaced by explicit (user_id, key) lookup with
# NULL-user_id fallback.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_personal_key_returns_system_default_when_no_user_row():
    """GET /api/config/user.timezone returns the system row value."""
    app, pool, conn, _ = _make_app()
    conn.fetchrow.return_value = FakeRecord(key="user.timezone", value="UTC")
    try:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/config/user.timezone")
        assert resp.status_code == 200
        assert resp.json()["value"] == "UTC"
    finally:
        app.dependency_overrides.clear()
        app.state.limiter.enabled = True
