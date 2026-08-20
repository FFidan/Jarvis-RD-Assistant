"""Platform configuration and provider event contract tests."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from httpx import ASGITransport
from jarvis_common import verify_api_key
from jarvis_common.auth import current_user_id_strict
from jarvis_common.identity_assertions import IdentityAssertionSigner
from jarvis_common.provider_test import ProviderTestResult
from jarvis_common.testing import SignedIdentityMiddleware

from platform_api.deps import get_db_pool, get_identity_signer, limiter
from platform_api.routers import configuration, providers
from platform_api.services import config_delivery as delivery_service
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


@pytest.fixture()
def _app():
    """Create a minimal Platform app with deterministic owner dependencies."""
    pool, conn = _make_pool_and_conn(
        direct_methods=True,
        execute_return="UPDATE 1",
        fetchval_return=None,
    )
    signer = IdentityAssertionSigner(
        issuer="jarvis-platform-test",
        key_id="settings-events",
        signing_key=Ed25519PrivateKey.generate(),
    )
    research_client = AsyncMock(spec=httpx.AsyncClient)

    async def apply_config(
        url: str,
        *,
        json: dict[str, object],
        **_kwargs: object,
    ) -> httpx.Response:
        key = url.rsplit("/", maxsplit=1)[-1]
        return httpx.Response(
            200,
            json={"key": key, "value": json["value"], "schedule_apply_warnings": []},
            request=httpx.Request("PUT", url),
        )

    research_client.put.side_effect = apply_config
    research_client.post.return_value = httpx.Response(
        204,
        request=httpx.Request(
            "POST",
            "http://paper_ingestion:8000/internal/platform/providers/openrouter/cache/invalidate",
        ),
    )
    app = FastAPI()
    app.include_router(configuration.router)
    app.include_router(providers.router)
    app.state.db_pool = pool
    app.state.http_client = research_client
    app.dependency_overrides[get_db_pool] = lambda: pool
    app.dependency_overrides[get_identity_signer] = lambda: signer
    app.dependency_overrides[current_user_id_strict] = lambda: 1
    app.dependency_overrides[verify_api_key] = lambda: None
    limiter_was_enabled = limiter.enabled
    limiter.enabled = False
    commands: list[delivery_service.ConfigCommand] = []
    validate_value = delivery_service.validate_value

    async def _validate(**kwargs: Any) -> None:
        commands.append(kwargs["command"])
        await validate_value(**kwargs)

    async def _deliver(**kwargs: Any) -> tuple[bool, dict[str, Any]]:
        payload = await delivery_service._send_value(
            client=kwargs["client"],
            signer=kwargs["signer"],
            command=commands[-1],
            phase="apply",
        )
        return True, payload

    with (
        patch.object(delivery_service, "validate_value", side_effect=_validate),
        patch.object(delivery_service, "deliver", side_effect=_deliver),
    ):
        try:
            yield app, conn, research_client
        finally:
            limiter.enabled = limiter_was_enabled


def _identity_app(app: FastAPI, role: str | None = "admin") -> SignedIdentityMiddleware:
    """Wrap the Platform app in deterministic browser identity state."""
    return SignedIdentityMiddleware(
        app,
        audience="research",
        user_id=1,
        role=role,
    )


@pytest.mark.asyncio
async def test_settings_save_emits_event(_app) -> None:
    """A confirmed configuration write records one Platform-owned event."""
    app, _conn, _research_client = _app
    event = AsyncMock()
    with patch.object(configuration, "log_event", new=event):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=_identity_app(app)),
            base_url="http://test",
        ) as client:
            response = await client.put(
                "/api/config/pulse.enabled",
                json={"key": "pulse.enabled", "value": True},
            )

    assert response.status_code == 200, response.text
    event.assert_awaited_once()
    kwargs = event.await_args.kwargs
    assert kwargs["category"] == "config"
    assert kwargs["message"] == "setting_changed"
    assert kwargs["context"] == {"key": "pulse.enabled", "new_value": "True"}


@pytest.mark.parametrize(("key", "value"), _UI_PREF_VALUES.items())
@pytest.mark.asyncio
async def test_non_admin_forwards_each_personal_preference(
    _app,
    key: str,
    value: object,
) -> None:
    """A member can forward each allowlisted personal preference to Research."""
    app, _conn, research_client = _app
    with patch.object(configuration, "log_event", new=AsyncMock()):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=_identity_app(app, "member")),
            base_url="http://test",
        ) as client:
            response = await client.put(
                f"/api/config/{key}",
                json={"key": key, "value": value},
            )

    assert response.status_code == 200, response.text
    assert research_client.put.await_count == 2
    assert research_client.put.await_args.kwargs["json"] == {
        "value": value,
        "phase": "apply",
        "zotero_scope_changed": False,
    }


@pytest.mark.asyncio
async def test_downstream_preference_validation_is_preserved(_app) -> None:
    """A safe Research validation failure remains a public 400 response."""
    app, conn, research_client = _app
    research_client.put.side_effect = None
    research_client.put.return_value = httpx.Response(
        400,
        json={"detail": "Invalid value for 'ui.nav_mode'"},
        request=httpx.Request(
            "PUT",
            "http://paper_ingestion:8000/internal/platform/config/ui.nav_mode",
        ),
    )
    async with httpx.AsyncClient(
        transport=ASGITransport(app=_identity_app(app, "member")),
        base_url="http://test",
    ) as client:
        response = await client.put(
            "/api/config/ui.nav_mode",
            json={"key": "ui.nav_mode", "value": "wide"},
        )

    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid value for 'ui.nav_mode'"}
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_preference_get_does_not_expose_another_users_value(_app) -> None:
    """A member cannot read a preference row belonging to another user."""
    app, conn, _research_client = _app
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
        transport=ASGITransport(app=_identity_app(app, "member")),
        base_url="http://test",
    ) as client:
        response = await client.get("/api/config/ui.nav_mode")

    assert response.status_code == 404
    assert conn.fetchrow.await_args.args[1:] == ("ui.nav_mode", 1)


@pytest.mark.asyncio
async def test_settings_event_failure_does_not_block_response(_app) -> None:
    """A best-effort event failure does not turn a confirmed write into failure."""
    app, _conn, _research_client = _app
    failing_event = AsyncMock(side_effect=RuntimeError("database unavailable"))
    with patch.object(configuration, "log_event", new=failing_event):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=_identity_app(app)),
            base_url="http://test",
        ) as client:
            response = await client.put(
                "/api/config/pulse.enabled",
                json={"key": "pulse.enabled", "value": True},
            )

    assert response.status_code == 200, response.text


@pytest.mark.asyncio
async def test_route_assignment_uses_dedicated_audit_and_event(_app) -> None:
    """Model assignments remain distinguishable from ordinary setting changes."""
    app, _conn, _research_client = _app
    event = AsyncMock()
    audit = AsyncMock()
    with (
        patch.object(configuration, "log_event", new=event),
        patch.object(configuration, "log_audit", new=audit),
    ):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=_identity_app(app)),
            base_url="http://test",
        ) as client:
            response = await client.put(
                "/api/config/llm.fast_model",
                json={"key": "llm.fast_model", "value": "qwen3:8b"},
            )

    assert response.status_code == 200, response.text
    assert audit.await_args.kwargs["action"] == "llm.route.change"
    assert audit.await_args.kwargs["resource"] == "llm.fast_model"
    assert event.await_args.kwargs["message"] == "llm/route_changed"
    assert event.await_args.kwargs["context"] == {"key": "llm.fast_model", "role": "fast"}


@pytest.mark.asyncio
async def test_provider_connection_test_emits_sanitized_event(_app) -> None:
    """Provider probe events retain only the provider and stable outcome code."""
    app, _conn, _research_client = _app
    event = AsyncMock()
    with (
        patch.object(providers, "_read_system_secret", new=AsyncMock(return_value="test-token")),
        patch.object(
            providers,
            "test_provider_connectivity",
            new=AsyncMock(
                return_value=ProviderTestResult(ok=False, error="provider request failed")
            ),
        ),
        patch.object(providers, "log_event", new=event),
    ):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=_identity_app(app)),
            base_url="http://test",
        ) as client:
            response = await client.post("/api/providers/openrouter/test")

    assert response.status_code == 200
    assert event.await_args.kwargs["message"] == "llm/provider_connection_checked"
    assert event.await_args.kwargs["context"] == {
        "provider": "openrouter",
        "success": False,
        "code": "connection_failed",
    }


@pytest.mark.asyncio
async def test_missing_provider_key_still_emits_connection_event(_app) -> None:
    """A missing credential remains a visible checked-connection outcome."""
    app, _conn, _research_client = _app
    event = AsyncMock()
    with (
        patch.object(providers, "_read_system_secret", new=AsyncMock(return_value=None)),
        patch.object(providers, "log_event", new=event),
    ):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=_identity_app(app)),
            base_url="http://test",
        ) as client:
            response = await client.post("/api/providers/openrouter/test")

    assert response.status_code == 200
    assert event.await_args.kwargs["context"] == {
        "provider": "openrouter",
        "success": False,
        "code": "api_key_unavailable",
    }
