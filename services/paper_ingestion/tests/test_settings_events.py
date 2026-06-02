"""Tests for LG-B4 B2: set_config emits config-change events.

Verifies that ``PUT /api/config/{key}`` calls ``log_event`` with
category='config', message='setting_changed' after a successful UPSERT.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from httpx import ASGITransport

from tests.conftest import _make_pool_and_conn


@pytest.fixture()
def _app():
    """Create a minimal app instance with mocked DB pool and disabled auth."""
    from jarvis_common import verify_api_key
    from jarvis_common.auth import current_user_id_strict, require_admin
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app
    from paper_ingestion.routers import settings as _settings_mod

    mock_pool, conn = _make_pool_and_conn()
    app.state.db_pool = mock_pool
    app.state.limiter.enabled = False
    mock_http = AsyncMock()
    app.state.http_client = mock_http

    app.dependency_overrides[get_db_pool] = lambda: mock_pool
    app.dependency_overrides[verify_api_key] = lambda: None
    # set_config resolves the caller via Depends(current_user_id_strict); steer it
    # to a concrete user (the route hard-401s sessionless callers otherwise).
    app.dependency_overrides[current_user_id_strict] = lambda: 1
    # Admin-gate the settings endpoints (see test_settings._app).
    app.dependency_overrides[require_admin] = lambda: None
    _orig_require_admin = _settings_mod.require_admin
    _settings_mod.require_admin = AsyncMock(return_value=None)
    yield app, conn, mock_http
    _settings_mod.require_admin = _orig_require_admin
    app.dependency_overrides.clear()
    app.state.limiter.enabled = True


@pytest.mark.asyncio
async def test_settings_save_emits_event(_app) -> None:
    """PUT /api/config/{key} emits log_event with category='config' and message='setting_changed'."""
    app, conn, _mock_http = _app
    # Mock DB UPSERT to succeed silently
    conn.execute = AsyncMock(return_value=None)
    # Mock fetchrow used for pulse.cron pre-read (not triggered here)
    conn.fetchrow = AsyncMock(return_value=None)
    # Use pulse.enabled (bool, non-ROLE_TO_ALIAS) to avoid Ollama model
    # validation HTTP calls. This keeps the test purely unit-level.

    log_event_mock = AsyncMock()
    with patch("paper_ingestion.routers.settings._log_event", new=log_event_mock):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.put(
                "/api/config/pulse.enabled",
                json={"key": "pulse.enabled", "value": True},
            )

    # A 200 means the endpoint succeeded
    assert resp.status_code == 200, f"Unexpected status: {resp.status_code} — {resp.text}"

    log_event_mock.assert_awaited()
    config_calls = [
        c
        for c in log_event_mock.await_args_list
        if c.kwargs.get("category") == "config" and c.kwargs.get("message") == "setting_changed"
    ]
    assert config_calls, (
        f"Expected log_event(category='config', message='setting_changed') call, "
        f"got calls: {[c.kwargs for c in log_event_mock.await_args_list]}"
    )
    config_kwargs = config_calls[0].kwargs
    assert config_kwargs["level"] == "info"
    assert config_kwargs["source"] == "settings"
    ctx_payload = config_kwargs.get("context", {})
    assert ctx_payload.get("key") == "pulse.enabled"


@pytest.mark.asyncio
async def test_settings_save_event_failure_does_not_block_response(_app) -> None:
    """If log_event raises, set_config still returns 200 (best-effort logging)."""
    app, conn, _mock_http = _app
    conn.execute = AsyncMock(return_value=None)
    conn.fetchrow = AsyncMock(return_value=None)

    failing_log_event = AsyncMock(side_effect=RuntimeError("DB connection lost"))
    with patch("paper_ingestion.routers.settings._log_event", new=failing_log_event):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.put(
                "/api/config/pulse.enabled",
                json={"key": "pulse.enabled", "value": True},
            )

    assert resp.status_code == 200, f"log_event failure should not block 200: {resp.text}"
