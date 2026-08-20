"""Platform gateway authorization and capability contracts."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, Literal, cast
from unittest.mock import AsyncMock

import asyncpg
import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from jarvis_common.auth import RAW_CLIENT_SCOPE_KEY, require_admin_or_api_key
from jarvis_common.identity_assertions import (
    IdentityAssertionSigner,
    IdentityAssertionVerifier,
    VerificationKey,
)
from jarvis_common.identity_capabilities import (
    ServicePrincipal,
    required_identity_scopes,
    service_principal_scopes,
)
from jarvis_common.identity_middleware import IdentityAssertionMiddleware
from jarvis_common.testing import SharedConnPool, make_pool_and_conn
from learning_engine.deps import get_db_pool as get_learning_db_pool
from learning_engine.routers import internal_domains as learning_internal_domains
from paper_ingestion.deps import get_db_pool as get_research_db_pool
from paper_ingestion.deps import get_scheduler as get_research_scheduler
from paper_ingestion.routers import internal_config
from paper_ingestion.routers import internal_domains as research_internal_domains
from paper_ingestion.services.config_write import ConfigWriteResult
from platform_api.auth_cookie_relay import AuthCookieRelayMiddleware
from platform_api.config import PlatformSettings
from platform_api.deps import (
    authenticate_service_principal,
    get_db_pool,
    get_identity_signer,
    get_service_principal_tokens,
    verify_platform_request,
)
from platform_api.routers import (
    configuration,
    internal_auth,
    internal_services,
    internal_telegram,
    providers,
    system,
)
from platform_api.service_principals import ServicePrincipalTokens
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

_HTTP_DELETE = "DELETE"


class _IdentityStateMiddleware:
    """Inject test session state and a raw transport peer into ASGI scope."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        raw_peer: str,
        include_session: bool,
    ) -> None:
        self.app = app
        self._raw_peer = raw_peer
        self._include_session = include_session

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Attach deterministic authentication state before routing the request."""
        if scope["type"] == "http":
            scope[RAW_CLIENT_SCOPE_KEY] = (self._raw_peer, 43210)
            if self._include_session:
                state = scope.setdefault("state", {})
                state.update(
                    user_id=7,
                    user_role="admin",
                    session_id="session-7",
                )
        await self.app(scope, receive, send)


def _build_client(
    *,
    raw_peer: str = "127.0.0.1",
    include_session: bool = True,
    configured_api_key: str = "",
) -> tuple[TestClient, IdentityAssertionVerifier]:
    private_key = Ed25519PrivateKey.generate()
    signer = IdentityAssertionSigner(
        issuer="jarvis-platform",
        key_id="current",
        signing_key=private_key,
    )
    verifier = IdentityAssertionVerifier(
        issuer="jarvis-platform",
        audience="research",
        keys={"current": VerificationKey(private_key.public_key())},
    )
    app = FastAPI()
    app.include_router(internal_auth.router)

    def signer_override() -> IdentityAssertionSigner:
        return signer

    def api_key_override() -> str:
        return configured_api_key

    def settings_override() -> PlatformSettings:
        return PlatformSettings()

    async def runtime_override() -> internal_auth._AuthorizationRuntime:
        return internal_auth._AuthorizationRuntime(
            signer=signer_override(),
            configured_api_key=api_key_override(),
            settings=settings_override(),
        )

    app.dependency_overrides[internal_auth._authorization_runtime] = runtime_override

    app.add_middleware(
        _IdentityStateMiddleware,
        raw_peer=raw_peer,
        include_session=include_session,
    )
    return TestClient(app), verifier


def _authorization_headers() -> dict[str, str]:
    return {
        "X-Jarvis-Target-Audience": "research",
        "X-Jarvis-Original-Method": "GET",
        "X-Jarvis-Original-Path": "/api/papers",
        "X-Request-Id": "request-7",
    }


def _build_setup_client(
    research_result: httpx.Response | BaseException,
    *,
    setup_row: dict[str, object] | None = None,
    telegram_token_row: dict[str, object] | None = None,
    telegram_paired: bool = False,
) -> tuple[TestClient, IdentityAssertionVerifier, AsyncMock]:
    private_key = Ed25519PrivateKey.generate()
    signer = IdentityAssertionSigner(
        issuer="jarvis-platform",
        key_id="current",
        signing_key=private_key,
    )
    verifier = IdentityAssertionVerifier(
        issuer="jarvis-platform",
        audience="research",
        keys={"current": VerificationKey(private_key.public_key())},
    )
    pool, _ = make_pool_and_conn(
        fetchrow_side_effects=[setup_row, telegram_token_row],
        fetchval_return=telegram_paired,
        with_transaction=False,
    )
    research_client = AsyncMock(spec=httpx.AsyncClient)
    if isinstance(research_result, BaseException):
        research_client.get.side_effect = research_result
    else:
        research_client.get.return_value = research_result

    app = FastAPI()
    app.include_router(system.router)
    app.state.http_client = research_client

    def signer_override() -> IdentityAssertionSigner:
        return signer

    def pool_override() -> asyncpg.Pool:
        return cast(asyncpg.Pool, pool)

    app.dependency_overrides[get_identity_signer] = signer_override
    app.dependency_overrides[get_db_pool] = pool_override
    app.add_middleware(
        _IdentityStateMiddleware,
        raw_peer="127.0.0.1",
        include_session=True,
    )
    return TestClient(app), verifier, research_client


def _build_internal_telegram_client(
    *,
    principal: ServicePrincipal = "telegram",
    **pool_options: Any,
) -> tuple[TestClient, IdentityAssertionVerifier, AsyncMock]:
    """Build the scoped Telegram boundary with deterministic auth and storage."""
    private_key = Ed25519PrivateKey.generate()
    signer = IdentityAssertionSigner(
        issuer="jarvis-platform",
        key_id="current",
        signing_key=private_key,
    )
    verifier = IdentityAssertionVerifier(
        issuer="jarvis-platform",
        audience="research",
        keys={"current": VerificationKey(private_key.public_key())},
    )
    pool, conn = make_pool_and_conn(direct_methods=True, **pool_options)
    app = FastAPI()
    app.include_router(internal_telegram.router)

    def principal_override() -> ServicePrincipal:
        return principal

    def signer_override() -> IdentityAssertionSigner:
        return signer

    def pool_override() -> asyncpg.Pool:
        return cast(asyncpg.Pool, pool)

    app.dependency_overrides[authenticate_service_principal] = principal_override
    app.dependency_overrides[get_identity_signer] = signer_override
    app.dependency_overrides[get_db_pool] = pool_override
    return TestClient(app), verifier, conn


def test_session_subrequest_returns_route_bound_assertion() -> None:
    client, verifier = _build_client()

    response = client.get("/internal/authorize", headers=_authorization_headers())

    assert response.status_code == 204
    claims = verifier.verify(
        response.headers["X-Jarvis-Identity"],
        required_scopes=("research:papers:read",),
        request_id="request-7",
        request_method="GET",
        request_path="/api/papers",
    )
    assert claims.principal == "browser"
    assert claims.user_id == 7
    assert claims.user_role == "admin"
    assert claims.session_id == "session-7"


def test_api_key_subrequest_carries_no_user_identity_and_no_admin_authority() -> None:
    client, verifier = _build_client(include_session=False, configured_api_key="operator-key")
    headers = {**_authorization_headers(), "X-API-Key": "operator-key"}

    response = client.get("/internal/authorize", headers=headers)

    assert response.status_code == 204
    claims = verifier.verify(
        response.headers["X-Jarvis-Identity"],
        required_scopes=("research:papers:read",),
        request_id="request-7",
        request_method="GET",
        request_path="/api/papers",
    )
    assert claims.principal == "api-key"
    assert claims.user_id is None
    assert claims.user_role is None
    assert claims.session_id is None
    assert _admin_gate_status(principal=claims.principal, user_role=claims.user_role) == 403


def _admin_gate_status(*, principal: str, user_role: str | None) -> int:
    """Return the status a backend admin route gives one signed identity.

    Parameters
    ----------
    principal : str
        Calling principal carried by the assertion.
    user_role : str or None
        Authenticated role carried by the assertion.

    Returns
    -------
    int
        Status code of a route guarded by ``require_admin_or_api_key`` behind
        the production identity middleware.
    """
    private_key = Ed25519PrivateKey.generate()
    signer = IdentityAssertionSigner(
        issuer="jarvis-platform",
        key_id="current",
        signing_key=private_key,
    )
    app = FastAPI()
    app.state.identity_verifier = IdentityAssertionVerifier(
        issuer="jarvis-platform",
        audience="research",
        keys={"current": VerificationKey(private_key.public_key())},
    )

    @app.get("/api/system/hardware", dependencies=[Depends(require_admin_or_api_key)])
    async def hardware() -> dict[str, bool]:
        return {"reachable": True}

    app.add_middleware(
        IdentityAssertionMiddleware,
        scope_resolver=lambda method, path: required_identity_scopes("research", method, path),
    )
    assertion = signer.issue(
        audience="research",
        subject="operator-api-key" if user_role is None else "user:7",
        principal=principal,
        user_id=None if user_role is None else 7,
        user_role=user_role,
        request_id="admin-gate",
        request_method="GET",
        request_path="/api/system/hardware",
        scopes=("research:system:read",),
    )
    response = TestClient(app).get(
        "/api/system/hardware",
        headers={"X-Jarvis-Identity": assertion, "X-Request-Id": "admin-gate"},
    )
    return response.status_code


def test_browser_admin_assertion_still_reaches_an_operations_route() -> None:
    """The api-key rule must not close the route to a real administrator."""
    assert _admin_gate_status(principal="browser", user_role="admin") == 200


def test_application_auth_defers_only_to_gateway_route_authentication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gateway route replaces, rather than duplicates, the app-wide check."""
    general_auth = AsyncMock()
    monkeypatch.setattr("platform_api.deps.verify_api_key", general_auth)
    private_key = Ed25519PrivateKey.generate()
    signer = IdentityAssertionSigner(
        issuer="jarvis-platform",
        key_id="current",
        signing_key=private_key,
    )
    app = FastAPI(dependencies=[Depends(verify_platform_request)])
    app.include_router(internal_auth.router)
    app.dependency_overrides[internal_auth._authorization_runtime] = lambda: (
        internal_auth._AuthorizationRuntime(
            signer=signer,
            configured_api_key="operator-key",
            settings=PlatformSettings(),
        )
    )
    app.add_middleware(
        _IdentityStateMiddleware,
        raw_peer="127.0.0.1",
        include_session=False,
    )

    response = TestClient(app).get(
        "/internal/authorize",
        headers={**_authorization_headers(), "X-API-Key": "operator-key"},
    )

    assert response.status_code == 204
    assert "X-Jarvis-Identity" in response.headers
    general_auth.assert_not_awaited()


@pytest.mark.parametrize(
    ("raw_peer", "include_session", "expected_status"),
    [
        ("192.0.2.10", True, 403),
        ("127.0.0.1", False, 401),
    ],
)
def test_subrequest_rejects_untrusted_peer_or_missing_identity(
    raw_peer: str,
    include_session: bool,
    expected_status: int,
) -> None:
    client, _ = _build_client(raw_peer=raw_peer, include_session=include_session)

    response = client.get("/internal/authorize", headers=_authorization_headers())

    assert response.status_code == expected_status
    assert "X-Jarvis-Identity" not in response.headers


def test_capability_classifier_refuses_every_route_it_does_not_recognize() -> None:
    """Only the named unprotected routes and preflight escape the boundary."""
    assert required_identity_scopes("research", "GET", "/api/papers") == ("research:papers:read",)
    assert required_identity_scopes("learning", "POST", "/api/cards") == ("learning:cards:write",)
    assert required_identity_scopes("research", "OPTIONS", "/api/papers") is None
    for unprotected_path in (
        "/health",
        "/health/live",
        "/health/internal",
        "/docs",
        "/docs/oauth2-redirect",
        "/openapi.json",
        "/redoc",
    ):
        assert required_identity_scopes("research", "GET", unprotected_path) is None
    assert required_identity_scopes("research", "PUT", "/internal/platform/config/pulse.cron") == (
        "research:config:write",
    )
    assert required_identity_scopes(
        "research",
        "POST",
        "/internal/platform/providers/openrouter/cache/invalidate",
    ) == ("research:providers:write",)
    assert required_identity_scopes("learning", "PUT", "/internal/domains/journal") == (
        "learning:domain:write",
    )
    for refused_path in (
        "/internal/domains/unlisted",
        "/internal/telegram/pairings",
        "/metrics",
        "/health/internal/detail",
    ):
        with pytest.raises(ValueError, match="outside the identity capability boundary"):
            required_identity_scopes("learning", "GET", refused_path)


def test_service_principal_manifest_is_exact_and_deny_by_default() -> None:
    assert service_principal_scopes("telegram", "research", "GET", "/api/papers/42") == (
        "research:papers:read",
    )
    assert service_principal_scopes("telegram", "learning", "POST", "/api/review/42") == (
        "learning:review:write",
    )
    assert service_principal_scopes("telegram", "learning", "GET", "/api/executive/my-day") == (
        "learning:executive:read",
    )
    assert service_principal_scopes("telegram", "learning", "POST", "/api/executive/my-day") is None
    assert service_principal_scopes("telegram", "research", "GET", "/api/admin/users") is None
    assert (
        service_principal_scopes("telegram", "learning", _HTTP_DELETE, "/api/projects/42") is None
    )
    assert service_principal_scopes("research", "learning", "GET", "/api/projects") is None
    assert service_principal_scopes(
        "learning", "research", "POST", "/internal/domains/library"
    ) == ("research:library:write",)
    assert (
        service_principal_scopes("research", "research", "POST", "/internal/domains/library")
        is None
    )
    assert (
        service_principal_scopes(
            "research", "research", "PUT", "/internal/platform/config/pulse.cron"
        )
        is None
    )
    assert (
        service_principal_scopes(
            "research",
            "research",
            "POST",
            "/internal/platform/providers/openrouter/cache/invalidate",
        )
        is None
    )


def _build_service_authorization_client(
    *,
    principal: ServicePrincipal,
    user_active: bool = True,
) -> tuple[TestClient, IdentityAssertionVerifier]:
    """Build the Platform signer boundary used by Research and Learning."""
    private_key = Ed25519PrivateKey.generate()
    signer = IdentityAssertionSigner(
        issuer="jarvis-platform",
        key_id="current",
        signing_key=private_key,
    )
    verifier = IdentityAssertionVerifier(
        issuer="jarvis-platform",
        audience="learning",
        keys={"current": VerificationKey(private_key.public_key())},
    )
    pool, _ = make_pool_and_conn(fetchval_return=user_active, direct_methods=True)
    app = FastAPI()
    app.include_router(internal_services.router)
    app.dependency_overrides[authenticate_service_principal] = lambda: principal
    app.dependency_overrides[get_identity_signer] = lambda: signer
    app.dependency_overrides[get_db_pool] = lambda: cast(asyncpg.Pool, pool)
    return TestClient(app), verifier


def test_service_authorization_names_a_subject_only_for_declared_owner_commands() -> None:
    """A named user is mintable only where the manifest says the caller may name one."""
    client, verifier = _build_service_authorization_client(principal="research")

    owner_command = client.post(
        "/internal/services/authorize",
        json={
            "audience": "learning",
            "method": "PUT",
            "path": "/internal/domains/journal",
            "request_id": "research-journal-7",
            "user_id": 7,
        },
    )

    assert owner_command.status_code == 200, owner_command.text
    payload = owner_command.json()
    assert payload["scopes"] == ["learning:domain:write"]
    claims = verifier.verify(
        payload["assertion"],
        required_scopes=("learning:domain:write",),
        request_id="research-journal-7",
        request_method="PUT",
        request_path="/internal/domains/journal",
    )
    assert claims.subject == "user:7"
    assert claims.user_id == 7

    paired_client, _ = _build_service_authorization_client(principal="telegram")
    borrowed = paired_client.post(
        "/internal/services/authorize",
        json={
            "audience": "learning",
            "method": "GET",
            "path": "/api/projects",
            "request_id": "telegram-borrowed-user",
            "user_id": 7,
        },
    )

    assert borrowed.status_code == 403
    assert borrowed.json() == {"detail": "Service command is not allowed"}


def test_internal_telegram_authorization_binds_paired_user_and_destination() -> None:
    client, verifier, conn = _build_internal_telegram_client(fetchval_return=True)

    response = client.post(
        "/internal/telegram/authorize",
        json={
            "audience": "research",
            "method": "GET",
            "path": "/api/papers/42",
            "request_id": "telegram-request-42",
            "user_id": 7,
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["user_id"] == 7
    assert body["scopes"] == ["research:papers:read"]
    claims = verifier.verify(
        body["assertion"],
        required_scopes=("research:papers:read",),
        request_id="telegram-request-42",
        request_method="GET",
        request_path="/api/papers/42",
    )
    assert claims.principal == "telegram"
    assert claims.subject == "user:7"
    assert claims.user_id == 7
    conn.fetchval.assert_awaited_once()


def test_internal_telegram_authorization_denies_unlisted_capability_before_storage() -> None:
    client, _, conn = _build_internal_telegram_client(fetchval_return=True)

    response = client.post(
        "/internal/telegram/authorize",
        json={
            "audience": "research",
            "method": "GET",
            "path": "/api/admin/users",
            "request_id": "telegram-denied",
            "user_id": 7,
        },
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "Telegram capability is not allowed"}
    conn.fetchval.assert_not_awaited()


def test_internal_telegram_authorization_requires_active_pairing() -> None:
    client, _, _ = _build_internal_telegram_client(fetchval_return=False)

    response = client.post(
        "/internal/telegram/authorize",
        json={
            "audience": "learning",
            "method": "GET",
            "path": "/api/projects",
            "request_id": "telegram-unpaired",
            "user_id": 7,
        },
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Telegram pairing not found"}


@pytest.mark.parametrize(
    ("principal", "token", "expected_status"),
    [
        ("telegram", "wrong-token", 401),
        ("research", "research-token", 403),
        ("telegram", "telegram-token", 200),
    ],
)
def test_internal_telegram_service_credentials_are_scoped(
    principal: str,
    token: str,
    expected_status: int,
) -> None:
    pool, _ = make_pool_and_conn(fetch_return=[], direct_methods=True)
    app = FastAPI()
    app.include_router(internal_telegram.router)
    configured = ServicePrincipalTokens(
        telegram="telegram-token",
        research="research-token",
        learning="learning-token",
    )

    def pool_override() -> asyncpg.Pool:
        return cast(asyncpg.Pool, pool)

    def tokens_override() -> ServicePrincipalTokens:
        return configured

    app.dependency_overrides[get_db_pool] = pool_override
    app.dependency_overrides[get_service_principal_tokens] = tokens_override

    response = TestClient(app).get(
        "/internal/telegram/pairings",
        headers={
            "X-Jarvis-Service-Principal": principal,
            "X-Jarvis-Service-Token": token,
        },
    )

    assert response.status_code == expected_status


def test_platform_application_auth_accepts_only_dedicated_internal_credentials() -> None:
    """The app-level dependency defers exact internal routes to service auth."""
    pool, _ = make_pool_and_conn(fetchrow_return=None, direct_methods=True)
    app = FastAPI(dependencies=[Depends(verify_platform_request)])
    app.include_router(internal_services.router)
    app.state.service_principal_tokens = ServicePrincipalTokens(
        telegram="telegram-token",
        research="research-token",
        learning="learning-token",
    )
    app.dependency_overrides[get_db_pool] = lambda: cast(asyncpg.Pool, pool)

    @app.get("/internal/services/unlisted")
    async def unlisted_internal_route() -> dict[str, bool]:
        return {"accepted": True}

    client = TestClient(app)
    body = {"roles": ["smart"], "pending": False}

    accepted = client.post(
        "/internal/services/research-config-effects",
        headers={
            "X-Jarvis-Service-Principal": "research",
            "X-Jarvis-Service-Token": "research-token",
        },
        json=body,
    )
    missing = client.post("/internal/services/research-config-effects", json=body)
    foreign = client.post(
        "/internal/services/research-config-effects",
        headers={
            "X-Jarvis-Service-Principal": "learning",
            "X-Jarvis-Service-Token": "learning-token",
        },
        json=body,
    )
    unlisted = client.get(
        "/internal/services/unlisted",
        headers={
            "X-Jarvis-Service-Principal": "research",
            "X-Jarvis-Service-Token": "research-token",
        },
    )

    assert accepted.status_code == 204
    assert missing.status_code == 401
    assert foreign.status_code == 403
    assert unlisted.status_code in {401, 403}


def test_internal_telegram_timer_preferences_returns_the_saved_values() -> None:
    """The bot reads the same focus length and daily target the web app stores."""
    client, _, _ = _build_internal_telegram_client(
        fetchval_return={
            "workMinutes": 45,
            "shortBreakMinutes": 5,
            "longBreakMinutes": 15,
            "targetCycles": 6,
        }
    )

    response = client.get("/internal/telegram/preferences/7/timer")

    assert response.status_code == 200, response.text
    assert response.json() == {"work_minutes": 45, "target_cycles": 6}


@pytest.mark.parametrize(
    "stored",
    [None, {"workMinutes": 5, "targetCycles": 99}, {"workMinutes": "45"}, "not-a-mapping"],
    ids=["absent", "out-of-range", "wrong-type", "unparsable"],
)
def test_internal_telegram_timer_preferences_falls_back_to_web_defaults(stored: object) -> None:
    """An absent or unusable preference resolves to the values the web timer would use."""
    client, _, _ = _build_internal_telegram_client(fetchval_return=stored)

    response = client.get("/internal/telegram/preferences/7/timer")

    assert response.status_code == 200, response.text
    assert response.json() == {"work_minutes": 25, "target_cycles": 4}


def test_internal_telegram_timer_preferences_reject_another_service() -> None:
    """Only the Telegram principal may read a user's saved timer preference."""
    client, _, conn = _build_internal_telegram_client(principal="research", fetchval_return=None)

    response = client.get("/internal/telegram/preferences/7/timer")

    assert response.status_code == 403
    conn.fetchval.assert_not_awaited()


def test_internal_telegram_pairing_reports_invalid_code_without_mutation() -> None:
    client, _, conn = _build_internal_telegram_client(fetchrow_return=None)

    response = client.post(
        "/internal/telegram/pairings",
        json={"token": "unknown-code", "chat_id": 42, "telegram_username": "alice"},
    )

    assert response.status_code == 200
    assert response.json() == {"outcome": "invalid", "user_id": None, "prior_chat_id": None}
    conn.execute.assert_not_awaited()


def test_subrequest_rejects_noncanonical_method_binding() -> None:
    client, _ = _build_client()
    headers = {**_authorization_headers(), "X-Jarvis-Original-Method": "get"}

    response = client.get("/internal/authorize", headers=headers)

    assert response.status_code == 400
    assert "X-Jarvis-Identity" not in response.headers


def test_platform_settings_reject_malformed_gateway_networks() -> None:
    settings = PlatformSettings(gateway_auth_allowed_cidrs="127.0.0.1/32,not-a-network")

    with pytest.raises(ValueError, match="not-a-network"):
        settings.gateway_auth_networks


def test_platform_setup_status_combines_owner_state_and_binds_research_assertion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    research_request = httpx.Request(
        "GET",
        "http://paper_ingestion:8000/api/system/setup-status/research",
    )
    research_response = httpx.Response(
        200,
        json={
            "models_ready": False,
            "models_downloading": ["smart"],
            "topics_count": 4,
            "model_warnings": ["Smart model is still downloading."],
        },
        request=research_request,
    )
    client, verifier, research_client = _build_setup_client(
        research_response,
        setup_row={"value": "true"},
        telegram_token_row={"value": None, "encrypted_value": b"ciphertext"},
        telegram_paired=True,
    )
    monkeypatch.setattr(
        system,
        "get_secrets_settings",
        lambda: SimpleNamespace(telegram_bot_token=None),
    )

    response = client.get("/api/system/setup-status")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "setup_completed": True,
        "models_ready": False,
        "models_downloading": ["smart"],
        "topics_count": 4,
        "telegram_configured": True,
        "telegram_paired": True,
        "model_warnings": ["Smart model is still downloading."],
    }
    downstream_call = research_client.get.await_args
    assert downstream_call is not None
    assert downstream_call.args == ("http://paper_ingestion:8000/api/system/setup-status/research",)
    assert downstream_call.kwargs["timeout"] == 10.0
    headers = cast(dict[str, str], downstream_call.kwargs["headers"])
    assert set(headers) == {"X-Jarvis-Identity", "X-Request-Id"}
    claims = verifier.verify(
        headers["X-Jarvis-Identity"],
        required_scopes=("research:system:read",),
        request_id=headers["X-Request-Id"],
        request_method="GET",
        request_path="/api/system/setup-status/research",
    )
    assert claims.subject == "user:7"
    assert claims.principal == "browser"
    assert claims.user_id == 7
    assert claims.user_role == "admin"
    assert claims.session_id == "session-7"


@pytest.mark.parametrize(
    ("telegram_token_row", "mounted_token", "expected"),
    [
        (None, None, False),
        ({"value": None, "encrypted_value": b"ciphertext"}, None, True),
        (None, "mounted-token", True),
    ],
)
def test_platform_setup_status_accepts_stored_or_mounted_telegram_token(
    monkeypatch: pytest.MonkeyPatch,
    telegram_token_row: dict[str, object] | None,
    mounted_token: str | None,
    expected: bool,
) -> None:
    research_response = httpx.Response(
        200,
        json={
            "models_ready": True,
            "models_downloading": [],
            "topics_count": 0,
            "model_warnings": [],
        },
        request=httpx.Request(
            "GET",
            "http://paper_ingestion:8000/api/system/setup-status/research",
        ),
    )
    client, _, _ = _build_setup_client(
        research_response,
        telegram_token_row=telegram_token_row,
    )
    monkeypatch.setattr(
        system,
        "get_secrets_settings",
        lambda: SimpleNamespace(telegram_bot_token=mounted_token),
    )

    response = client.get("/api/system/setup-status")

    assert response.status_code == 200, response.text
    assert response.json()["telegram_configured"] is expected


@pytest.mark.parametrize(
    "research_result",
    [
        httpx.Response(
            200,
            json={"models_ready": True},
            request=httpx.Request(
                "GET",
                "http://paper_ingestion:8000/api/system/setup-status/research",
            ),
        ),
        httpx.ConnectError(
            "Research is unavailable",
            request=httpx.Request(
                "GET",
                "http://paper_ingestion:8000/api/system/setup-status/research",
            ),
        ),
    ],
)
def test_platform_setup_status_fails_closed_for_invalid_or_unavailable_research(
    monkeypatch: pytest.MonkeyPatch,
    research_result: httpx.Response | BaseException,
) -> None:
    client, _, _ = _build_setup_client(research_result)
    monkeypatch.setattr(
        system,
        "get_secrets_settings",
        lambda: SimpleNamespace(telegram_bot_token=None),
    )

    response = client.get("/api/system/setup-status")

    assert response.status_code == 503
    assert response.json() == {"detail": "Research setup readiness is unavailable"}


def test_auth_cookie_relay_preserves_multiple_cookie_headers() -> None:
    app = FastAPI()

    @app.get("/internal/authorize")
    async def authorize() -> Response:
        response = Response(status_code=204)
        response.headers.append("Set-Cookie", "jarvis_session=renewed; HttpOnly; Path=/")
        response.headers.append("Set-Cookie", "jarvis_device=known; SameSite=Strict; Path=/")
        return response

    app.add_middleware(AuthCookieRelayMiddleware)

    response = TestClient(app).get("/internal/authorize")

    assert "set-cookie" not in response.headers
    assert response.headers["X-Jarvis-Set-Cookie-1"].startswith("jarvis_session=renewed")
    assert response.headers["X-Jarvis-Set-Cookie-2"].startswith("jarvis_device=known")


def test_auth_cookie_relay_fails_closed_when_cookie_bound_is_exceeded() -> None:
    app = FastAPI()

    @app.get("/internal/authorize")
    async def authorize() -> Response:
        response = Response(status_code=204)
        response.headers.append("Set-Cookie", "first=1")
        response.headers.append("Set-Cookie", "second=2")
        return response

    app.add_middleware(AuthCookieRelayMiddleware, maximum_cookies=1)

    response = TestClient(app).get("/internal/authorize")

    assert response.status_code == 500
    assert "set-cookie" not in response.headers
    assert "X-Jarvis-Set-Cookie-1" not in response.headers


def _build_platform_config_client(
    research_result: httpx.Response | BaseException,
) -> tuple[TestClient, IdentityAssertionVerifier, AsyncMock]:
    """Build the Platform configuration boundary with deterministic collaborators."""
    private_key = Ed25519PrivateKey.generate()
    signer = IdentityAssertionSigner(
        issuer="jarvis-platform",
        key_id="current",
        signing_key=private_key,
    )
    verifier = IdentityAssertionVerifier(
        issuer="jarvis-platform",
        audience="research",
        keys={"current": VerificationKey(private_key.public_key())},
    )
    pool, conn = make_pool_and_conn(
        with_transaction=False,
        direct_methods=True,
        execute_return="UPDATE 1",
    )
    conn.fetchrow.return_value = {
        "delivery_id": uuid.uuid4(),
        "scope_user_id": 0,
        "user_id": 7,
        "user_role": "admin",
        "session_id": "session-7",
        "zotero_scope_changed": False,
        "key": "pulse.cron",
        "value": "0 4 * * *",
        "encrypted_value": None,
        "attempts": 0,
        "next_attempt_at": datetime.now(UTC),
    }
    research_client = AsyncMock(spec=httpx.AsyncClient)
    if isinstance(research_result, BaseException):
        research_client.put.side_effect = research_result
    else:
        research_client.put.return_value = research_result

    app = FastAPI()
    app.include_router(configuration.router)
    app.state.http_client = research_client

    def signer_override() -> IdentityAssertionSigner:
        return signer

    def pool_override() -> asyncpg.Pool:
        return cast(asyncpg.Pool, pool)

    app.dependency_overrides[get_identity_signer] = signer_override
    app.dependency_overrides[get_db_pool] = pool_override
    app.add_middleware(
        _IdentityStateMiddleware,
        raw_peer="127.0.0.1",
        include_session=True,
    )
    return TestClient(app), verifier, research_client


def test_platform_config_write_binds_exact_research_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Platform forwards a write with the caller and exact route cryptographically bound."""
    research_response = httpx.Response(
        200,
        json={
            "key": "pulse.cron",
            "value": "0 4 * * *",
            "schedule_apply_warnings": [],
        },
        request=httpx.Request(
            "PUT",
            "http://paper_ingestion:8000/internal/platform/config/pulse.cron",
        ),
    )
    client, verifier, research_client = _build_platform_config_client(research_response)
    monkeypatch.setattr(configuration, "log_audit", AsyncMock())
    monkeypatch.setattr(configuration, "log_event", AsyncMock())

    response = client.put(
        "/api/config/pulse.cron",
        json={"key": "ignored-by-path-contract", "value": "0 4 * * *"},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "key": "pulse.cron",
        "value": "0 4 * * *",
        "delivery_state": "applied",
    }
    assert research_client.put.await_count == 2
    validation_call, delivery_call = research_client.put.await_args_list
    assert validation_call.kwargs["json"] == {
        "value": "0 4 * * *",
        "phase": "validate",
        "zotero_scope_changed": False,
    }
    assert delivery_call.kwargs["json"] == {
        "value": "0 4 * * *",
        "phase": "apply",
        "zotero_scope_changed": False,
    }
    downstream_call = delivery_call
    assert downstream_call.args == (
        "http://paper_ingestion:8000/internal/platform/config/pulse.cron",
    )
    assert downstream_call.kwargs["timeout"] == 310.0
    headers = cast(dict[str, str], downstream_call.kwargs["headers"])
    claims = verifier.verify(
        headers["X-Jarvis-Identity"],
        required_scopes=("research:config:write",),
        request_id=headers["X-Request-Id"],
        request_method="PUT",
        request_path="/internal/platform/config/pulse.cron",
    )
    assert claims.principal == "browser"
    assert claims.user_id == 7
    assert claims.user_role == "admin"
    assert claims.session_id == "session-7"


@pytest.mark.parametrize(
    "research_result",
    [
        httpx.Response(
            200,
            json={"key": "different.key", "value": True},
            request=httpx.Request(
                "PUT",
                "http://paper_ingestion:8000/internal/platform/config/pulse.enabled",
            ),
        ),
        httpx.ConnectError(
            "Research is unavailable",
            request=httpx.Request(
                "PUT",
                "http://paper_ingestion:8000/internal/platform/config/pulse.enabled",
            ),
        ),
    ],
)
def test_platform_config_write_fails_closed_for_invalid_or_unavailable_research(
    monkeypatch: pytest.MonkeyPatch,
    research_result: httpx.Response | BaseException,
) -> None:
    """No success response is emitted without a matching Research confirmation."""
    client, _, _ = _build_platform_config_client(research_result)
    audit = AsyncMock()
    event = AsyncMock()
    monkeypatch.setattr(configuration, "log_audit", audit)
    monkeypatch.setattr(configuration, "log_event", event)

    response = client.put(
        "/api/config/pulse.enabled",
        json={"key": "pulse.enabled", "value": True},
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "Configuration update is temporarily unavailable"}
    audit.assert_not_awaited()
    event.assert_not_awaited()


def _build_research_config_client() -> tuple[TestClient, IdentityAssertionSigner]:
    """Build the signed Research command boundary with a real verifier."""
    private_key = Ed25519PrivateKey.generate()
    signer = IdentityAssertionSigner(
        issuer="jarvis-platform",
        key_id="current",
        signing_key=private_key,
    )
    verifier = IdentityAssertionVerifier(
        issuer="jarvis-platform",
        audience="research",
        keys={"current": VerificationKey(private_key.public_key())},
    )
    pool, _ = make_pool_and_conn(with_transaction=False)
    app = FastAPI()
    app.include_router(internal_config.router)
    app.state.http_client = AsyncMock(spec=httpx.AsyncClient)
    app.state.identity_verifier = verifier

    def pool_override() -> asyncpg.Pool:
        return cast(asyncpg.Pool, pool)

    def scheduler_override() -> object:
        return object()

    app.dependency_overrides[get_research_db_pool] = pool_override
    app.dependency_overrides[get_research_scheduler] = scheduler_override
    app.add_middleware(
        IdentityAssertionMiddleware,
        scope_resolver=lambda method, path: required_identity_scopes("research", method, path),
    )
    return TestClient(app), signer


def _build_domain_command_client(
    *,
    audience: Literal["learning", "research"],
    router: APIRouter,
    pool_dependency: Callable[..., Any],
) -> tuple[TestClient, IdentityAssertionSigner]:
    """Build one owner-command router behind the production identity middleware."""
    private_key = Ed25519PrivateKey.generate()
    signer = IdentityAssertionSigner(
        issuer="jarvis-platform",
        key_id="current",
        signing_key=private_key,
    )
    verifier = IdentityAssertionVerifier(
        issuer="jarvis-platform",
        audience=audience,
        keys={"current": VerificationKey(private_key.public_key())},
    )
    pool, _ = make_pool_and_conn(with_transaction=False)
    app = FastAPI()
    app.include_router(router)
    app.state.identity_verifier = verifier
    app.dependency_overrides[pool_dependency] = lambda: cast(asyncpg.Pool, pool)
    app.add_middleware(
        IdentityAssertionMiddleware,
        scope_resolver=lambda method, path: required_identity_scopes(audience, method, path),
    )
    return TestClient(app), signer


def test_research_erasure_routes_require_the_exact_signed_user() -> None:
    """Research accepts only Platform commands whose body matches the assertion."""
    client, signer = _build_domain_command_client(
        audience="research",
        router=research_internal_domains.router,
        pool_dependency=get_research_db_pool,
    )
    request_uuid = uuid.uuid4()
    path = f"/internal/domains/erasure/{request_uuid}/research"

    def call(*, asserted_user_id: int, body_user_id: int, request_id: str) -> httpx.Response:
        assertion = signer.issue(
            audience="research",
            subject="service:platform",
            principal="platform",
            user_id=asserted_user_id,
            request_id=request_id,
            request_method="POST",
            request_path=path,
            scopes=("research:erasure:write",),
        )
        return client.post(
            path,
            json={"user_id": body_user_id},
            headers={"X-Jarvis-Identity": assertion, "X-Request-Id": request_id},
        )

    accepted = call(asserted_user_id=7, body_user_id=7, request_id="research-erasure-ok")
    denied = call(asserted_user_id=8, body_user_id=7, request_id="research-erasure-mismatch")

    assert accepted.status_code == 200, accepted.text
    assert denied.status_code == 403


def test_research_library_command_binds_learning_subject_and_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Research accepts only the exact signed Learning library command."""
    client, signer = _build_domain_command_client(
        audience="research",
        router=research_internal_domains.router,
        pool_dependency=get_research_db_pool,
    )
    ownership = AsyncMock()
    add_to_library = AsyncMock()
    monkeypatch.setattr(research_internal_domains, "assert_paper_ownership", ownership)
    monkeypatch.setattr(research_internal_domains, "add_to_library", add_to_library)
    path = "/internal/domains/library"

    def call(
        *,
        asserted_user_id: int,
        body_user_id: int,
        request_id: uuid.UUID,
        body_request_id: uuid.UUID | None = None,
    ) -> httpx.Response:
        assertion = signer.issue(
            audience="research",
            subject="service:learning",
            principal="learning",
            user_id=asserted_user_id,
            request_id=str(request_id),
            request_method="POST",
            request_path=path,
            scopes=("research:library:write",),
        )
        return client.post(
            path,
            json={
                "request_id": str(body_request_id or request_id),
                "user_id": body_user_id,
                "paper_id": 42,
            },
            headers={"X-Jarvis-Identity": assertion, "X-Request-Id": str(request_id)},
        )

    accepted = call(asserted_user_id=7, body_user_id=7, request_id=uuid.uuid4())
    denied_subject = call(asserted_user_id=8, body_user_id=7, request_id=uuid.uuid4())
    denied_request = call(
        asserted_user_id=7,
        body_user_id=7,
        request_id=uuid.uuid4(),
        body_request_id=uuid.uuid4(),
    )

    assert accepted.status_code == 200, accepted.text
    assert accepted.json() == {"acknowledged": True}
    assert denied_subject.status_code == 403
    assert denied_request.status_code == 403
    ownership.assert_awaited_once()
    add_to_library.assert_awaited_once()


def test_learning_owner_routes_require_the_exact_signed_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Learning rejects a Research command for a different asserted subject."""
    client, signer = _build_domain_command_client(
        audience="learning",
        router=learning_internal_domains.router,
        pool_dependency=get_learning_db_pool,
    )
    apply_command = AsyncMock(return_value=True)
    monkeypatch.setattr(learning_internal_domains, "apply_command", apply_command)
    path = "/internal/domains/paper-read"

    def call(
        *,
        asserted_user_id: int,
        body_user_id: int,
        request_id: uuid.UUID,
        body_request_id: uuid.UUID | None = None,
    ) -> httpx.Response:
        request_id_text = str(request_id)
        assertion = signer.issue(
            audience="learning",
            subject="service:research",
            principal="research",
            user_id=asserted_user_id,
            request_id=request_id_text,
            request_method="POST",
            request_path=path,
            scopes=("learning:domain:write",),
        )
        return client.post(
            path,
            json={
                "request_id": str(body_request_id or request_id),
                "user_id": body_user_id,
                "paper_id": 42,
            },
            headers={"X-Jarvis-Identity": assertion, "X-Request-Id": request_id_text},
        )

    accepted = call(asserted_user_id=7, body_user_id=7, request_id=uuid.uuid4())
    denied = call(asserted_user_id=8, body_user_id=7, request_id=uuid.uuid4())
    mismatched_request = call(
        asserted_user_id=7,
        body_user_id=7,
        request_id=uuid.uuid4(),
        body_request_id=uuid.uuid4(),
    )

    assert accepted.status_code == 200, accepted.text
    assert denied.status_code == 403
    assert mismatched_request.status_code == 403
    apply_command.assert_awaited_once()


def test_research_config_command_requires_and_uses_verified_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Research rejects unsigned calls and applies the caller from a valid assertion."""
    client, signer = _build_research_config_client()
    write = AsyncMock(
        return_value=ConfigWriteResult(
            display_value="0 4 * * *",
            schedule_apply_warnings=[],
        )
    )
    monkeypatch.setattr(internal_config, "write_config", write)
    path = "/internal/platform/config/pulse.cron"

    unsigned = client.put(path, json={"value": "0 4 * * *"})
    request_id = "config-command-7"
    assertion = signer.issue(
        audience="research",
        subject="user:7",
        principal="browser",
        user_id=7,
        user_role="admin",
        session_id="session-7",
        request_id=request_id,
        request_method="PUT",
        request_path=path,
        scopes=("research:config:write",),
    )
    signed = client.put(
        path,
        json={"value": "0 4 * * *"},
        headers={
            "X-Jarvis-Identity": assertion,
            "X-Request-Id": request_id,
        },
    )

    assert unsigned.status_code == 401
    assert signed.status_code == 200, signed.text
    assert signed.json() == {
        "key": "pulse.cron",
        "value": "0 4 * * *",
        "schedule_apply_warnings": [],
        "litellm_delivery_roles": [],
        "litellm_delivery_pending": None,
        "effective_num_ctx_role": None,
        "effective_num_ctx_value": None,
    }
    assert write.await_args is not None
    assert write.await_args.kwargs["caller_user_id"] == 7


def test_research_provider_config_write_invalidates_only_its_runtime_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A confirmed provider setting change invalidates its Research model cache."""
    client, signer = _build_research_config_client()
    write = AsyncMock(
        return_value=ConfigWriteResult(
            display_value="****oken",
            schedule_apply_warnings=[],
        )
    )
    invalidate = AsyncMock()
    monkeypatch.setattr(internal_config, "write_config", write)
    monkeypatch.setattr(internal_config, "invalidate_provider_model_cache", invalidate)
    key = "llm.providers.openrouter.api_key"
    path = f"/internal/platform/config/{key}"
    request_id = "provider-config-write"
    assertion = signer.issue(
        audience="research",
        subject="user:7",
        principal="browser",
        user_id=7,
        user_role="admin",
        session_id="session-7",
        request_id=request_id,
        request_method="PUT",
        request_path=path,
        scopes=("research:config:write",),
    )

    response = client.put(
        path,
        json={"value": "test-token"},
        headers={"X-Jarvis-Identity": assertion, "X-Request-Id": request_id},
    )

    assert response.status_code == 200, response.text
    invalidate.assert_awaited_once_with("openrouter")


def test_research_provider_cache_command_requires_exact_admin_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Research accepts only the exact Platform-signed administrator command."""
    client, signer = _build_research_config_client()
    invalidate = AsyncMock()
    monkeypatch.setattr(internal_config, "invalidate_provider_model_cache", invalidate)
    path = "/internal/platform/providers/openrouter/cache/invalidate"

    unsigned = client.post(path)
    wrong_scope_id = "provider-cache-wrong-scope"
    wrong_scope = signer.issue(
        audience="research",
        subject="user:7",
        principal="browser",
        user_id=7,
        user_role="admin",
        session_id="session-7",
        request_id=wrong_scope_id,
        request_method="POST",
        request_path=path,
        scopes=("research:config:write",),
    )
    denied_scope = client.post(
        path,
        headers={
            "X-Jarvis-Identity": wrong_scope,
            "X-Request-Id": wrong_scope_id,
        },
    )
    member_id = "provider-cache-member"
    member = signer.issue(
        audience="research",
        subject="user:8",
        principal="browser",
        user_id=8,
        user_role="member",
        session_id="session-8",
        request_id=member_id,
        request_method="POST",
        request_path=path,
        scopes=("research:providers:write",),
    )
    denied_member = client.post(
        path,
        headers={"X-Jarvis-Identity": member, "X-Request-Id": member_id},
    )
    admin_id = "provider-cache-admin"
    admin = signer.issue(
        audience="research",
        subject="user:7",
        principal="browser",
        user_id=7,
        user_role="admin",
        session_id="session-7",
        request_id=admin_id,
        request_method="POST",
        request_path=path,
        scopes=("research:providers:write",),
    )
    accepted = client.post(
        path,
        headers={"X-Jarvis-Identity": admin, "X-Request-Id": admin_id},
    )

    assert unsigned.status_code == 401
    assert denied_scope.status_code == 401
    assert denied_member.status_code == 403
    assert accepted.status_code == 204
    invalidate.assert_awaited_once_with("openrouter")


def _build_platform_provider_client(
    research_result: httpx.Response | BaseException,
) -> tuple[TestClient, IdentityAssertionVerifier, AsyncMock, AsyncMock]:
    """Build the Platform provider boundary with deterministic collaborators."""
    private_key = Ed25519PrivateKey.generate()
    signer = IdentityAssertionSigner(
        issuer="jarvis-platform",
        key_id="current",
        signing_key=private_key,
    )
    verifier = IdentityAssertionVerifier(
        issuer="jarvis-platform",
        audience="research",
        keys={"current": VerificationKey(private_key.public_key())},
    )
    pool, conn = make_pool_and_conn(fetch_return=[], with_transaction=False)
    research_client = AsyncMock(spec=httpx.AsyncClient)
    if isinstance(research_result, BaseException):
        research_client.post.side_effect = research_result
    else:
        research_client.post.return_value = research_result

    app = FastAPI()
    app.include_router(providers.router)
    app.state.http_client = research_client
    app.dependency_overrides[get_identity_signer] = lambda: signer
    app.dependency_overrides[get_db_pool] = lambda: cast(asyncpg.Pool, pool)
    app.dependency_overrides[providers.verify_api_key] = lambda: None
    app.add_middleware(
        _IdentityStateMiddleware,
        raw_peer="127.0.0.1",
        include_session=True,
    )
    return TestClient(app), verifier, research_client, conn


def test_platform_provider_removal_binds_exact_research_cache_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Platform binds cache acknowledgement before deleting its provider row."""
    downstream = httpx.Response(
        204,
        request=httpx.Request(
            "POST",
            "http://paper_ingestion:8000/internal/platform/providers/openrouter/cache/invalidate",
        ),
    )
    client, verifier, research_client, conn = _build_platform_provider_client(downstream)
    monkeypatch.setattr(providers, "log_audit", AsyncMock())

    response = client.delete("/api/providers/openrouter/key")

    assert response.status_code == 204, response.text
    downstream_call = research_client.post.await_args
    assert downstream_call is not None
    path = "/internal/platform/providers/openrouter/cache/invalidate"
    assert downstream_call.args == (f"http://paper_ingestion:8000{path}",)
    headers = cast(dict[str, str], downstream_call.kwargs["headers"])
    claims = verifier.verify(
        headers["X-Jarvis-Identity"],
        required_scopes=("research:providers:write",),
        request_id=headers["X-Request-Id"],
        request_method="POST",
        request_path=path,
    )
    assert claims.user_id == 7
    assert claims.user_role == "admin"
    delete_calls = [
        call for call in conn.execute.await_args_list if "DELETE FROM user_config" in call.args[0]
    ]
    assert len(delete_calls) == 1


def test_platform_provider_removal_fails_closed_when_research_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed Research acknowledgement leaves the Platform setting intact."""
    failure = httpx.ConnectError(
        "Research is unavailable",
        request=httpx.Request(
            "POST",
            "http://paper_ingestion:8000/internal/platform/providers/openrouter/cache/invalidate",
        ),
    )
    client, _, _, conn = _build_platform_provider_client(failure)
    audit = AsyncMock()
    monkeypatch.setattr(providers, "log_audit", audit)

    response = client.delete("/api/providers/openrouter/key")

    assert response.status_code == 503
    assert response.json() == {"detail": "Provider settings are temporarily unavailable"}
    conn.execute.assert_not_awaited()
    audit.assert_not_awaited()


def test_public_platform_routes_exist_only_on_platform() -> None:
    """Moved public URLs are absent from Research and complete on Platform."""
    from paper_ingestion.main import app as research_app
    from platform_api.main import app as platform_app

    platform_paths = platform_app.openapi()["paths"]
    research_paths = research_app.openapi()["paths"]

    assert set(platform_paths["/api/config"]) == {"get"}
    assert set(platform_paths["/api/config/{key}"]) == {"get", "put"}
    assert "/api/config" not in research_paths
    assert "/api/config/{key}" not in research_paths
    assert set(research_paths["/internal/platform/config/{key}"]) == {"put"}
    assert set(platform_paths["/api/providers"]) == {"get"}
    assert set(platform_paths["/api/providers/{provider}/account"]) == {"get"}
    assert set(platform_paths["/api/providers/{provider}/test"]) == {"post"}
    assert set(platform_paths["/api/providers/{provider}/key"]) == {_HTTP_DELETE.lower()}
    assert set(platform_paths["/api/providers/{provider}/base-url"]) == {_HTTP_DELETE.lower()}
    assert all(not path.startswith("/api/providers") for path in research_paths)
    assert set(research_paths["/internal/platform/providers/{provider}/cache/invalidate"]) == {
        "post"
    }


@pytest.mark.contract
@pytest.mark.real_auth
@pytest.mark.asyncio(loop_scope="session")
async def test_telegram_reads_exclude_a_soft_deleted_paired_user(contract_conn: Any) -> None:
    """Deleting an account closes every Telegram path its chat still feeds.

    The pairing row deliberately outlives a soft delete so a restore keeps it,
    which means each reader has to consult the account state. One that does not
    keeps minting assertions for the deleted user and keeps the orchestration
    push paths delivering to their chat.
    """
    pool = cast(asyncpg.Pool, SharedConnPool(contract_conn))
    signer = IdentityAssertionSigner(
        issuer="jarvis-platform",
        key_id="current",
        signing_key=Ed25519PrivateKey.generate(),
    )
    user_id = await contract_conn.fetchval(
        """INSERT INTO platform.users (email, role, deleted_at)
           VALUES ($1, 'user', NOW()) RETURNING id""",
        f"paired-deleted-{uuid.uuid4().hex}@example.com",
    )
    chat_id = 900_000_000 + int(user_id)
    await contract_conn.execute(
        """INSERT INTO platform.telegram_user_pairings (user_id, chat_id, telegram_username)
           VALUES ($1, $2, 'deleted-account')""",
        user_id,
        chat_id,
    )
    await contract_conn.execute(
        """INSERT INTO platform.user_config (user_id, key, value)
           VALUES (NULL, 'telegram.owner_chat_id', to_jsonb($1::bigint))
           ON CONFLICT (user_id, key) DO UPDATE SET value = EXCLUDED.value""",
        chat_id,
    )

    with pytest.raises(HTTPException) as authorization:
        await internal_telegram.authorize_downstream_request(
            internal_telegram.TelegramAuthorizationRequest(
                audience="research",
                method="GET",
                path="/api/papers/42",
                request_id="telegram-deleted-account",
                user_id=user_id,
            ),
            "telegram",
            pool,
            signer,
        )
    assert authorization.value.status_code == 404

    with pytest.raises(HTTPException) as resolution:
        await internal_telegram.resolve_pairing(chat_id, "telegram", pool)
    assert resolution.value.status_code == 404

    listed = await internal_telegram.list_pairings("telegram", pool)
    assert user_id not in [record.user_id for record in listed]

    runtime = await internal_telegram.get_telegram_runtime("telegram", pool)
    assert runtime.owner_user_id is None
