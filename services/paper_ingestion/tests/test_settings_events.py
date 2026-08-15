"""Tests that set_config emits config-change events.

Verifies that ``PUT /api/config/{key}`` calls ``log_event`` with
category='config', message='setting_changed' after a successful UPSERT.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from httpx import ASGITransport
from jarvis_common.testing import RoleMiddleware
from jarvis_common.testing_contract_apps import PITestAppOptions, patch_pi_test_app

from tests.conftest import _make_pool_and_conn

_UI_PREF_VALUES = {
    "ui.appearance": {
        "theme": "dark",
        "accent": "forest",
        "type": "editorial",
        "density": "compact",
    },
    "ui.timer": {
        "workMinutes": 50,
        "shortBreakMinutes": 10,
        "longBreakMinutes": 25,
        "targetCycles": 6,
    },
    "ui.nav_mode": "full",
}
_MALFORMED_UI_PREF_VALUES = {
    "ui.appearance": {"theme": "dark"},
    "ui.timer": {"workMinutes": 50},
    "ui.nav_mode": "wide",
}


@pytest.fixture()
def _app():
    """Create a minimal app instance with mocked DB pool and disabled auth."""
    from jarvis_common import verify_api_key
    from jarvis_common.auth import current_user_id_strict, require_admin
    from paper_ingestion.deps import get_db_pool, limiter
    from paper_ingestion.main import app
    from paper_ingestion.routers import settings as _settings_mod

    mock_pool, conn = _make_pool_and_conn()
    mock_http = AsyncMock()

    # The module-symbol swap is a seam the shared helper deliberately does not
    # cover; restore it manually alongside the helper's scoped restore.
    _orig_require_admin = _settings_mod.require_admin
    _settings_mod.require_admin = AsyncMock(return_value=None)
    try:
        with patch_pi_test_app(
            mock_pool,
            app=app,
            get_db_pool=get_db_pool,
            limiter=limiter,
            options=PITestAppOptions(
                remove_owner_override=False,
                override_db_dependency=True,
                disable_limiter=True,
                state_overrides={"http_client": mock_http},
                dependency_overrides={
                    verify_api_key: lambda: None,
                    # set_config resolves the caller via
                    # Depends(current_user_id_strict); steer it to a concrete
                    # user (the route hard-401s sessionless callers otherwise).
                    current_user_id_strict: lambda: 1,
                    # Admin-gate the settings endpoints (see test_settings._app).
                    require_admin: lambda: None,
                },
            ),
        ):
            yield app, conn, mock_http
    finally:
        _settings_mod.require_admin = _orig_require_admin


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


@pytest.mark.parametrize(("key", "value"), _UI_PREF_VALUES.items())
@pytest.mark.asyncio
async def test_non_admin_writes_each_preference_to_own_row(_app, key: str, value: object) -> None:
    """A regular browser session writes each interface preference under its user id."""
    from paper_ingestion.routers import settings as settings_module

    app, conn, _mock_http = _app
    conn.execute = AsyncMock(return_value=None)
    conn.fetchrow = AsyncMock(return_value=None)
    settings_module.require_admin.reset_mock()

    with patch("paper_ingestion.routers.settings._log_event", new=AsyncMock()):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=RoleMiddleware(app, "user")),
            base_url="http://test",
        ) as client:
            response = await client.put(
                f"/api/config/{key}",
                json={"key": key, "value": value},
            )

    assert response.status_code == 200, response.text
    settings_module.require_admin.assert_not_awaited()
    bound_writes = [call.args[1:] for call in conn.execute.await_args_list]
    assert (1, key, value) in bound_writes


@pytest.mark.parametrize(("key", "value"), _MALFORMED_UI_PREF_VALUES.items())
@pytest.mark.asyncio
async def test_malformed_preference_is_rejected_before_write(_app, key: str, value: object) -> None:
    """Malformed interface preferences return 400 without a database write."""
    app, conn, _mock_http = _app
    conn.execute = AsyncMock(return_value=None)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=RoleMiddleware(app, "user")),
        base_url="http://test",
    ) as client:
        response = await client.put(
            f"/api/config/{key}",
            json={"key": key, "value": value},
        )

    assert response.status_code == 400, response.text
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_preference_get_does_not_expose_another_users_value(_app) -> None:
    """A regular browser session cannot read another user's preference row."""
    app, conn, _mock_http = _app
    other_users_row = {
        "key": "ui.nav_mode",
        "value": "full",
        "encrypted_value": None,
        "user_id": 2,
    }

    async def fetchrow_for_user(_query: str, key: str, user_id: int):
        return other_users_row if (key, user_id) == ("ui.nav_mode", 2) else None

    conn.fetchrow = AsyncMock(side_effect=fetchrow_for_user)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=RoleMiddleware(app, "user")),
        base_url="http://test",
    ) as client:
        response = await client.get("/api/config/ui.nav_mode")

    assert response.status_code == 404, response.text
    assert conn.fetchrow.await_args.args[1:] == ("ui.nav_mode", 1)


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


@pytest.mark.asyncio
async def test_route_assignment_uses_dedicated_audit_and_event(_app) -> None:
    """Quick/Main assignments are distinguishable from ordinary setting changes."""
    from paper_ingestion.services.config_write import ConfigWriteResult

    app, _conn, _mock_http = _app
    write_config_mock = AsyncMock(return_value=ConfigWriteResult(display_value="qwen3:8b"))
    event_mock = AsyncMock()
    audit_mock = AsyncMock()
    with (
        patch("paper_ingestion.routers.settings.write_config", new=write_config_mock),
        patch("paper_ingestion.routers.settings._log_event", new=event_mock),
        patch("paper_ingestion.routers.settings.log_audit", new=audit_mock),
    ):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.put(
                "/api/config/llm.fast_model",
                json={"key": "llm.fast_model", "value": "qwen3:8b"},
            )

    assert response.status_code == 200
    assert audit_mock.await_args.kwargs["action"] == "llm.route.change"
    assert audit_mock.await_args.kwargs["resource"] == "llm.fast_model"
    event_kwargs = event_mock.await_args.kwargs
    assert event_kwargs["message"] == "llm/route_changed"
    assert event_kwargs["context"] == {"key": "llm.fast_model", "role": "fast"}


@pytest.mark.parametrize(
    ("key", "provider_id"),
    [
        ("llm.providers.openrouter.api_key", "openrouter"),
        ("llm.providers.custom_openai_compatible.base_url", "custom_openai_compatible"),
    ],
)
@pytest.mark.asyncio
async def test_provider_config_change_invalidates_only_its_model_cache(
    _app, key: str, provider_id: str
) -> None:
    from paper_ingestion.services.config_write import ConfigWriteResult

    app, _conn, _mock_http = _app
    write_config_mock = AsyncMock(return_value=ConfigWriteResult(display_value="configured"))
    with (
        patch("paper_ingestion.routers.settings.write_config", new=write_config_mock),
        patch(
            "paper_ingestion.routers.settings.invalidate_provider_model_cache",
            new=AsyncMock(),
        ) as invalidate_mock,
    ):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.put(
                f"/api/config/{key}",
                json={"key": key, "value": "replacement"},
            )

    assert response.status_code == 200
    invalidate_mock.assert_awaited_once_with(provider_id)


@pytest.mark.asyncio
async def test_provider_connection_test_emits_sanitized_event(_app) -> None:
    """Connection events identify the provider and stable outcome without a secret or body."""
    from paper_ingestion.services.provider_test import ProviderTestResult

    app, conn, _mock_http = _app
    conn.fetchrow.return_value = {"value": "ignored", "encrypted_value": None}
    event_mock = AsyncMock()
    with (
        patch("paper_ingestion.routers.settings.resolve_secret_row", return_value="test-token"),
        patch(
            "paper_ingestion.routers.settings.test_provider_connectivity",
            new=AsyncMock(
                return_value=ProviderTestResult(ok=False, error="provider request failed")
            ),
        ),
        patch("paper_ingestion.routers.settings._log_event", new=event_mock),
    ):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/api/providers/openrouter/test")

    assert response.status_code == 200
    event_kwargs = event_mock.await_args.kwargs
    assert event_kwargs["message"] == "llm/provider_connection_checked"
    assert event_kwargs["context"] == {
        "provider": "openrouter",
        "success": False,
        "code": "connection_failed",
    }


@pytest.mark.asyncio
async def test_missing_provider_key_still_emits_connection_event(_app) -> None:
    """A local configuration failure is a checked connection outcome, not silent state."""
    app, conn, _mock_http = _app
    conn.fetchrow.return_value = None
    event_mock = AsyncMock()
    with patch("paper_ingestion.routers.settings._log_event", new=event_mock):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/api/providers/openrouter/test")

    assert response.status_code == 200
    assert event_mock.await_args.kwargs["context"] == {
        "provider": "openrouter",
        "success": False,
        "code": "api_key_unavailable",
    }
