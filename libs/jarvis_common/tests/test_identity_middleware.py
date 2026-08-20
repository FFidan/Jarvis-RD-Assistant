"""Behavioral tests for the signed-identity ASGI middleware."""

from __future__ import annotations

from datetime import UTC, datetime

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from jarvis_common.identity_assertions import (
    IdentityAssertionSigner,
    IdentityAssertionVerifier,
    VerificationKey,
)
from jarvis_common.identity_middleware import IdentityAssertionMiddleware


def _app_and_signer(
    *,
    install_state_verifier: bool = True,
) -> tuple[FastAPI, IdentityAssertionSigner]:
    private_key = Ed25519PrivateKey.generate()
    signer = IdentityAssertionSigner(
        issuer="jarvis-platform",
        key_id="current-key",
        signing_key=private_key,
    )
    verifier = IdentityAssertionVerifier(
        issuer="jarvis-platform",
        audience="research",
        keys={"current-key": VerificationKey(private_key.public_key())},
    )
    app = FastAPI()
    if install_state_verifier:
        app.state.identity_verifier = verifier

    @app.get("/api/papers")
    async def papers(request: Request) -> dict[str, object]:
        return {
            "user_id": request.state.user_id,
            "role": request.state.user_role,
            "principal": request.state.identity_principal,
            "scopes": request.state.identity_scopes,
        }

    @app.get("/health/live")
    async def live() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/api/cookie-check")
    async def cookie_check(request: Request) -> dict[str, bool]:
        return {"cookie_present": "cookie" in request.headers}

    def resolve_scopes(method: str, path: str) -> tuple[str, ...] | None:
        if method == "GET" and path in {"/api/cookie-check", "/api/papers"}:
            return ("papers:read",)
        return None

    app.add_middleware(
        IdentityAssertionMiddleware,
        scope_resolver=resolve_scopes,
    )
    return app, signer


def _token(
    signer: IdentityAssertionSigner,
    *,
    request_id: str = "request-1",
    request_path: str = "/api/papers",
) -> str:
    return signer.issue(
        audience="research",
        subject="user:42",
        principal="browser",
        request_id=request_id,
        request_method="GET",
        request_path=request_path,
        scopes=("papers:read",),
        user_id=42,
        user_role="admin",
        session_id="session-1",
        now=datetime.now(UTC),
    )


def test_valid_assertion_populates_request_state() -> None:
    app, signer = _app_and_signer()

    response = TestClient(app).get(
        "/api/papers",
        headers={
            "X-Jarvis-Identity": _token(signer),
            "X-Request-ID": "request-1",
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "user_id": 42,
        "role": "admin",
        "principal": "browser",
        "scopes": ["papers:read"],
    }


def test_protected_route_rejects_missing_assertion() -> None:
    app, _ = _app_and_signer()

    response = TestClient(app).get("/api/papers", headers={"X-Request-ID": "request-1"})

    assert response.status_code == 401
    assert response.json() == {"detail": "Authentication required"}


def test_direct_forged_identity_header_is_rejected() -> None:
    app, _ = _app_and_signer()

    response = TestClient(app).get(
        "/api/papers",
        headers={
            "X-Jarvis-User-ID": "42",
            "X-Owner-User-ID": "42",
            "X-API-Key": "generic-key-must-not-establish-user-identity",
            "X-Request-ID": "request-1",
        },
    )

    assert response.status_code == 401


def test_valid_assertion_with_conflicting_owner_header_is_rejected() -> None:
    app, signer = _app_and_signer()

    response = TestClient(app).get(
        "/api/papers",
        headers={
            "X-Jarvis-Identity": _token(signer),
            "X-Owner-User-ID": "7",
            "X-Request-ID": "request-1",
        },
    )

    assert response.status_code == 401


def test_request_id_mismatch_is_rejected() -> None:
    app, signer = _app_and_signer()

    response = TestClient(app).get(
        "/api/papers",
        headers={
            "X-Jarvis-Identity": _token(signer, request_id="signed-request"),
            "X-Request-ID": "different-request",
        },
    )

    assert response.status_code == 401


def test_duplicate_assertion_headers_are_rejected() -> None:
    app, signer = _app_and_signer()
    token = _token(signer)

    response = TestClient(app).get(
        "/api/papers",
        headers=[
            ("X-Jarvis-Identity", token),
            ("X-Jarvis-Identity", token),
            ("X-Request-ID", "request-1"),
        ],
    )

    assert response.status_code == 401


def test_unprotected_health_route_needs_no_assertion() -> None:
    app, _ = _app_and_signer()

    response = TestClient(app).get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_a_route_the_classifier_refuses_is_not_served() -> None:
    """A route the capability classifier cannot place must fail closed.

    The classifier raises for any path it does not recognize, which is what
    makes an unlisted internal route refuse rather than serve unauthenticated.
    That decision lives entirely in the middleware's handling of the error, so
    without this the whole boundary rests on an untested except branch.
    """
    app = FastAPI()

    @app.get("/internal/unlisted")
    async def unlisted() -> dict[str, bool]:
        return {"served": True}

    def refuse_everything(method: str, path: str) -> tuple[str, ...] | None:
        raise ValueError("request path is outside the identity capability boundary")

    app.add_middleware(IdentityAssertionMiddleware, scope_resolver=refuse_everything)

    response = TestClient(app).get("/internal/unlisted")

    assert response.status_code == 401, (
        "an unclassifiable route was served — the classifier's refusal is being "
        "treated as 'no identity required'"
    )
    assert response.json() != {"served": True}


def test_missing_state_verifier_fails_closed() -> None:
    app, signer = _app_and_signer(install_state_verifier=False)

    response = TestClient(app).get(
        "/api/papers",
        headers={
            "X-Jarvis-Identity": _token(signer),
            "X-Request-ID": "request-1",
        },
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "Identity verification unavailable"}


def test_valid_assertion_removes_cookie_before_inner_middleware() -> None:
    app, signer = _app_and_signer()

    response = TestClient(app).get(
        "/api/cookie-check",
        headers={
            "Cookie": "jarvis_session=must-not-reach-research",
            "X-Jarvis-Identity": _token(signer, request_path="/api/cookie-check"),
            "X-Request-ID": "request-1",
        },
    )

    assert response.status_code == 200
    assert response.json() == {"cookie_present": False}
