"""ASGI middleware for verified Platform identity assertions."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from jarvis_common.identity_assertions import (
    IdentityAssertionError,
    IdentityAssertionVerifier,
    IdentityClaims,
)

IDENTITY_ASSERTION_HEADER = b"x-jarvis-identity"
REQUEST_ID_HEADER = b"x-request-id"

_FORBIDDEN_IDENTITY_HEADERS = frozenset(
    {
        b"x-jarvis-principal",
        b"x-jarvis-scopes",
        b"x-jarvis-session-id",
        b"x-jarvis-user-id",
        b"x-jarvis-user-role",
        b"x-owner-user-id",
    }
)

ScopeResolver = Callable[[str, str], tuple[str, ...] | None]


@dataclass(frozen=True, slots=True)
class _VerificationResult:
    """Represent one protected-route verification decision."""

    claims: IdentityClaims | None = None
    unprotected: bool = False
    status_code: int = 401
    detail: str = "Authentication required"


class IdentityAssertionMiddleware:
    """Require and verify Platform identity on protected backend routes.

    Parameters
    ----------
    app : ASGIApp
        Wrapped backend application.
    verifier : IdentityAssertionVerifier
        Optional destination-specific verifier with its own replay cache. When
        omitted, the middleware reads ``app.state.identity_verifier`` so key
        loading can remain a fail-fast lifespan responsibility.
    scope_resolver : ScopeResolver
        Callable receiving ``(method, path)``. It returns the route's required
        scopes or ``None`` for routes that do not require an assertion.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        verifier: IdentityAssertionVerifier | None = None,
        scope_resolver: ScopeResolver,
    ) -> None:
        """Store the verifier and route-scope resolver."""
        self.app = app
        self._verifier = verifier
        self._scope_resolver = scope_resolver

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Verify protected requests and populate their ASGI state."""
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        headers = list(scope.get("headers", []))
        method = str(scope.get("method", "GET")).upper()
        path = str(scope.get("path", ""))
        result = self._verify_request(scope, headers, method, path)
        if result.unprotected:
            await self.app(scope, receive, send)
            return
        if result.claims is None:
            await _reject(
                scope,
                send,
                status_code=result.status_code,
                detail=result.detail,
            )
            return

        claims = result.claims
        state = scope.setdefault("state", {})
        if claims.user_id is not None:
            state["user_id"] = claims.user_id
        if claims.user_role is not None:
            state["user_role"] = claims.user_role
        if claims.session_id is not None:
            state["session_id"] = claims.session_id
        state["identity_principal"] = claims.principal
        state["identity_scopes"] = claims.scopes
        state["identity_token_id"] = claims.token_id
        scope["headers"] = [(name, value) for name, value in headers if name.lower() != b"cookie"]
        await self.app(scope, receive, send)

    def _verify_request(
        self,
        scope: Scope,
        headers: list[tuple[bytes, bytes]],
        method: str,
        path: str,
    ) -> _VerificationResult:
        """Verify one request and return a transport-neutral decision."""
        if any(name.lower() in _FORBIDDEN_IDENTITY_HEADERS for name, _ in headers):
            return _VerificationResult()
        try:
            required_scopes = self._scope_resolver(method, path)
        except ValueError:
            return _VerificationResult()
        if required_scopes is None:
            return _VerificationResult(unprotected=True)

        verifier = self._verifier or _state_verifier(scope)
        if verifier is None:
            return _VerificationResult(
                status_code=503,
                detail="Identity verification unavailable",
            )

        assertion = _single_header(headers, IDENTITY_ASSERTION_HEADER)
        request_id = _single_header(headers, REQUEST_ID_HEADER)
        if assertion is None or request_id is None:
            return _VerificationResult()
        try:
            claims = verifier.verify(
                assertion,
                required_scopes=required_scopes,
                request_id=request_id,
                request_method=method,
                request_path=path,
            )
        except IdentityAssertionError:
            return _VerificationResult()
        return _VerificationResult(claims=claims)


def _single_header(headers: list[tuple[bytes, bytes]], name: bytes) -> str | None:
    values = [value for header_name, value in headers if header_name.lower() == name]
    if len(values) != 1:
        return None
    try:
        return values[0].decode("ascii")
    except UnicodeDecodeError:
        return None


def _state_verifier(scope: Scope) -> IdentityAssertionVerifier | None:
    app = scope.get("app")
    state = getattr(app, "state", None)
    verifier = getattr(state, "identity_verifier", None)
    if isinstance(verifier, IdentityAssertionVerifier):
        return verifier
    return None


async def _reject(
    scope: Scope,
    send: Send,
    *,
    status_code: int = 401,
    detail: str = "Authentication required",
) -> None:
    if scope["type"] == "websocket":
        await send({"type": "websocket.close", "code": 4401, "reason": "Authentication required"})
        return
    body = json.dumps({"detail": detail}, separators=(",", ":")).encode()
    start: Message = {
        "type": "http.response.start",
        "status": status_code,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
        ],
    }
    await send(start)
    await send({"type": "http.response.body", "body": body})


__all__ = [
    "IDENTITY_ASSERTION_HEADER",
    "REQUEST_ID_HEADER",
    "IdentityAssertionMiddleware",
    "ScopeResolver",
]
