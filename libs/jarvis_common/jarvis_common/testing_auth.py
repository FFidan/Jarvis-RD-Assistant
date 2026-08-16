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
from starlette.types import ASGIApp, Receive, Scope, Send

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
    verifier_app : Any
        FastAPI application whose state the backend middleware reads.
    user_id : int or None, default=1
        Authenticated test user. ``None`` models an API-key principal without a
        browser identity.
    role : str or None, optional
        Browser role embedded in the assertion.

    Notes
    -----
    This helper is explicit rather than automatic. Tests of missing, malformed,
    or replayed assertions continue to call the backend app directly. It also
    mirrors the verified state fields so route tests remain valid when the
    repository test harness intentionally disables destination middleware.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        audience: IdentityAudience,
        verifier_app: Any,
        user_id: int | None = 1,
        role: str | None = None,
    ) -> None:
        """Create and install one ephemeral signer-verifier pair."""
        private_key = Ed25519PrivateKey.generate()
        self._app = app
        self._audience: IdentityAudience = audience
        self._user_id = user_id
        self._role = role
        self._signer = IdentityAssertionSigner(
            issuer="jarvis-platform-test",
            key_id="ephemeral-test-key",
            signing_key=private_key,
        )
        verifier_app.state.identity_verifier = IdentityAssertionVerifier(
            issuer="jarvis-platform-test",
            audience=audience,
            keys={"ephemeral-test-key": VerificationKey(private_key.public_key())},
        )

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

        request_id = str(uuid.uuid4())
        principal = "browser" if self._user_id is not None else "api-key"
        subject = f"user:{self._user_id}" if self._user_id is not None else "api-key"
        assertion = self._signer.issue(
            audience=self._audience,
            subject=subject,
            principal=principal,
            user_id=self._user_id,
            user_role=self._role,
            request_id=request_id,
            request_method=method,
            request_path=path,
            scopes=scopes,
        )
        forwarded = dict(scope)
        state = dict(scope.get("state", {}))
        if self._user_id is not None:
            state["user_id"] = self._user_id
        if self._role is not None:
            state["user_role"] = self._role
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
        current_user_id_strict_with_owner_override,
        get_current_user_id,
    )

    resolver_names = (
        "current_user_id_strict",
        "current_user_id_strict_with_owner_override",
    )
    saved: list[tuple[object, str, object]] = []
    for mod_info in pkgutil.iter_modules(routers_pkg.__path__):
        module = importlib.import_module(f"{routers_pkg.__name__}.{mod_info.name}")
        for name in resolver_names:
            if hasattr(module, name):
                saved.append((module, name, getattr(module, name)))
                setattr(module, name, AsyncMock(return_value=1))

    # dependency_overrides is keyed by the resolver OBJECT, and FastAPI resolves
    # it per sub-dependant. Overriding get_current_user_id intercepts exactly the
    # routes that declare it; routes wired straight to current_user_id_strict
    # keep resolving for real, so a test wanting a specific identity there must
    # still say so itself.
    override_keys = (current_user_id_strict_with_owner_override, get_current_user_id)
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
