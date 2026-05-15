"""WS-AUTH-KEY-SESSION — a valid session passes the verify_api_key global gate.

A browser authenticated by SessionMiddleware (request.state.user_id set) must
pass verify_api_key without an X-API-Key header. The X-API-Key path,
dev-bypass path, and 401/403 no-credential path stay unchanged for callers
without a session. Identity/authz is still enforced downstream per-route.

Conventions follow test_auth_and_error_handlers.py: real starlette Request
built from a scope, _CACHED_API_KEY monkeypatched.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from starlette.requests import Request

_KEY = "correct-key-1234567890abcdefghij"


def _make_request(path: str = "/api/dashboard/metrics", *, user_id: object = None) -> Request:
    scope: dict = {
        "type": "http",
        "method": "GET",
        "path": path,
        "query_string": b"",
        "headers": [],
    }
    if user_id is not None:
        # Starlette stores request.state in scope["state"].
        scope["state"] = {"user_id": user_id}
    return Request(scope)


async def test_no_session_no_key_raises_403(monkeypatch) -> None:
    """Server has a key configured; no session + no key → 403 (unchanged)."""
    import jarvis_common.auth as auth_mod

    monkeypatch.setattr(auth_mod, "_CACHED_API_KEY", _KEY)
    with pytest.raises(HTTPException) as exc:
        await auth_mod.verify_api_key(_make_request(), None)
    assert exc.value.status_code == 403


async def test_no_session_correct_key_passes(monkeypatch) -> None:
    """No session + correct X-API-Key → passes (unchanged)."""
    import jarvis_common.auth as auth_mod

    monkeypatch.setattr(auth_mod, "_CACHED_API_KEY", _KEY)
    await auth_mod.verify_api_key(_make_request(), _KEY)


async def test_valid_session_no_key_passes(monkeypatch) -> None:
    """Valid session (request.state.user_id set) + no X-API-Key → passes."""
    import jarvis_common.auth as auth_mod

    monkeypatch.setattr(auth_mod, "_CACHED_API_KEY", _KEY)
    # Must not raise even though no API key header is supplied.
    await auth_mod.verify_api_key(_make_request(user_id=42), None)


async def test_unset_user_id_no_key_raises_403(monkeypatch) -> None:
    """Expired/revoked/deleted-user session → user_id unset → falls to 403."""
    import jarvis_common.auth as auth_mod

    monkeypatch.setattr(auth_mod, "_CACHED_API_KEY", _KEY)
    # No user_id in scope state — simulates SessionMiddleware leaving it unset.
    with pytest.raises(HTTPException) as exc:
        await auth_mod.verify_api_key(_make_request(), None)
    assert exc.value.status_code == 403


async def test_session_does_not_override_wrong_key_when_absent(monkeypatch) -> None:
    """Sanity: wrong key + no session → still 403 (session path not taken)."""
    import jarvis_common.auth as auth_mod

    monkeypatch.setattr(auth_mod, "_CACHED_API_KEY", _KEY)
    with pytest.raises(HTTPException) as exc:
        await auth_mod.verify_api_key(_make_request(), "wrong-key")
    assert exc.value.status_code == 403


async def test_exempt_path_returns_early_without_session_or_key(monkeypatch) -> None:
    """Exempt paths (e.g. /api/auth/*, health) return early regardless."""
    import jarvis_common.auth as auth_mod

    monkeypatch.setattr(auth_mod, "_CACHED_API_KEY", _KEY)
    for path in ("/health", "/api/auth/request-link", "/api/setup/status", "/infra-events"):
        await auth_mod.verify_api_key(_make_request(path), None)
