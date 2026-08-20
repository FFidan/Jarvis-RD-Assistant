"""Shared auth test infrastructure: ASGI ``RoleMiddleware`` shim and
``_default_authenticated_user`` autouse fixture helper.

Authentication-focused helpers extracted from ``jarvis_common.testing``.
"""

from __future__ import annotations

__all__ = ["RoleMiddleware", "SignedIdentityMiddleware", "_apply_default_authenticated_user"]

import uuid
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from starlette.types import Receive, Scope, Send

from jarvis_common.identity_assertions import (
    IdentityAssertionSigner,
    IdentityAssertionVerifier,
    VerificationKey,
)
from jarvis_common.identity_capabilities import IdentityAudience, required_identity_scopes

if TYPE_CHECKING:
    from types import ModuleType


class RoleMiddleware:
    """Minimal ASGI middleware that injects request.state.user_role before routing.

    When ``role`` is ``None`` the attribute is deliberately left absent,
    which exercises the API-key path in admin-gate tests.
    """

    def __init__(self, app: Any, role: str | None) -> None:
        """Wrap *app* and inject *role* into every HTTP request's state."""
        self._app = app
        self._role = role

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        """Set ``request.state.user_role`` before forwarding the ASGI call."""
        if scope["type"] == "http" and self._role is not None:
            from starlette.requests import Request

            request = Request(scope)
            request.state.user_role = self._role
        await self._app(scope, receive, send)


class SignedIdentityMiddleware:
    """Issue exact Platform-style assertions around a backend test app.

    Parameters
    ----------
    app : ASGIApp
        Backend application that enforces :class:`IdentityAssertionMiddleware`.
    audience : {"learning", "research"}
        Destination audience used for capability classification and signing.
    user_id : int or None, default=1
        Authenticated test user. ``None`` models an API-key principal without a
        browser identity.
    role : str or None, optional
        Browser role embedded in the assertion.
    session_pool : Any or None, optional
        Platform-scoped contract pool used to resolve the request's browser
        session. When supplied, its user and role replace the fixed values.

    Notes
    -----
    This helper is explicit rather than automatic. Tests of missing, malformed,
    or replayed assertions continue to call the backend app directly. It also
    mirrors the verified state fields so route tests remain valid when the
    repository test harness intentionally disables destination middleware.
    """

    def __init__(
        self,
        app: FastAPI,
        *,
        audience: IdentityAudience,
        user_id: int | None = 1,
        role: str | None = None,
        session_pool: Any | None = None,
    ) -> None:
        """Create and install one ephemeral signer-verifier pair."""
        private_key = Ed25519PrivateKey.generate()
        self._app = app
        self._audience: IdentityAudience = audience
        self._user_id = user_id
        self._role = role
        self._session_pool = session_pool
        self._signer = IdentityAssertionSigner(
            issuer="jarvis-platform-test",
            key_id="ephemeral-test-key",
            signing_key=private_key,
        )
        app.state.identity_verifier = IdentityAssertionVerifier(
            issuer="jarvis-platform-test",
            audience=audience,
            keys={"ephemeral-test-key": VerificationKey(private_key.public_key())},
        )

    def __getattr__(self, name: str) -> Any:
        """Delegate application attributes used by contract fixtures."""
        return getattr(self._app, name)

    async def _request_identity(self, scope: Scope) -> tuple[int | None, str | None]:
        """Resolve a browser cookie through Platform when configured."""
        if self._session_pool is None:
            return self._user_id, self._role

        from starlette.requests import Request

        raw_cookie = Request(scope).cookies.get("jarvis_session")
        if raw_cookie is None:
            return None, None
        try:
            session_id = uuid.UUID(raw_cookie)
        except ValueError:
            return None, None
        row = await self._session_pool.fetchrow(
            """SELECT s.user_id, u.role
               FROM platform.sessions AS s
               JOIN platform.users AS u ON u.id = s.user_id
               WHERE s.id = $1 AND s.revoked_at IS NULL
                 AND s.expires_at > NOW() AND u.deleted_at IS NULL""",
            session_id,
        )
        if row is None:
            return None, None
        return int(row["user_id"]), str(row["role"])

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Sign each protected HTTP request for its exact method and path."""
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        method = str(scope.get("method", "GET")).upper()
        path = str(scope.get("path", ""))
        scopes = required_identity_scopes(self._audience, method, path)
        if scopes is None:
            await self._app(scope, receive, send)
            return

        user_id, role = await self._request_identity(scope)
        request_id = str(uuid.uuid4())
        principal = "browser" if user_id is not None else "api-key"
        subject = f"user:{user_id}" if user_id is not None else "api-key"
        assertion = self._signer.issue(
            audience=self._audience,
            subject=subject,
            principal=principal,
            user_id=user_id,
            user_role=role,
            request_id=request_id,
            request_method=method,
            request_path=path,
            scopes=scopes,
        )
        forwarded = dict(scope)
        state = dict(scope.get("state", {}))
        if user_id is not None:
            state["user_id"] = user_id
        if role is not None:
            state["user_role"] = role
        state["identity_principal"] = principal
        state["identity_scopes"] = scopes
        forwarded["state"] = state
        headers = [
            (name, value)
            for name, value in scope.get("headers", [])
            if name.lower() not in {b"x-jarvis-identity", b"x-request-id"}
        ]
        headers.extend(
            [
                (b"x-jarvis-identity", assertion.encode("ascii")),
                (b"x-request-id", request_id.encode("ascii")),
            ]
        )
        forwarded["headers"] = headers
        await self._app(forwarded, receive, send)


@contextmanager
def _apply_default_authenticated_user(app: Any, routers_pkg: ModuleType):
    """Context manager: patch all strict user-id resolvers in *routers_pkg* to return 1.

    Intended for use inside a per-service autouse pytest fixture. Both the
    module-level symbol monkeypatch (for direct router-body callers) and the
    ``app.dependency_overrides`` entry (for ``Depends``-wired routes) are
    restored on exit.

    Parameters
    ----------
    app:
        The FastAPI application whose ``dependency_overrides`` to patch.
    routers_pkg:
        The service's ``routers`` package (e.g. ``paper_ingestion.routers``).
    """
    import importlib
    import pkgutil
    from unittest.mock import AsyncMock

    from jarvis_common.auth import (
        current_user_id_strict,
        get_current_user_id,
    )

    resolver_names = ("current_user_id_strict",)
    saved: list[tuple[object, str, object]] = []
    for mod_info in pkgutil.iter_modules(routers_pkg.__path__):
        module = importlib.import_module(f"{routers_pkg.__name__}.{mod_info.name}")
        for name in resolver_names:
            if hasattr(module, name):
                saved.append((module, name, getattr(module, name)))
                setattr(module, name, AsyncMock(return_value=1))

    # dependency_overrides is keyed by the resolver object, and FastAPI resolves
    # it per sub-dependant. Override both strict seams so app-level and direct
    # route dependencies receive the default test user consistently.
    override_keys = (current_user_id_strict, get_current_user_id)
    added = [key for key in override_keys if key not in app.dependency_overrides]
    for key in added:
        app.dependency_overrides[key] = lambda: 1
    try:
        yield
    finally:
        for module, name, original in saved:
            setattr(module, name, original)
        for key in added:
            app.dependency_overrides.pop(key, None)
