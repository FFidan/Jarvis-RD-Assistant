"""WS-AUTH-KEY-SESSION: POST /api/auth/api-key-session unit tests.

Mocks the DB pool so the suite runs without Docker, mirroring
``test_auth_magic_link.py`` conventions. Verifies the three guardrails:

1. Owner binding — OWNER_USER_ID setting else lowest-id admin; never created.
2. Audit + rate-limit — ``auth.api_key.session.minted`` / ``.failure``.
3. Single-tenant gate — multi-user + no opt-in → 403 magic-link message.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import paper_ingestion.routers.auth as auth_router
import pytest
from fastapi import HTTPException, Response
from jarvis_common.auth import refresh_api_key_cache

_KEY = "k" * 40


def _build_mock_pool(conn: AsyncMock) -> MagicMock:
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx
    return pool


def _build_request(pool: MagicMock) -> SimpleNamespace:
    app = SimpleNamespace(state=SimpleNamespace(db_pool=pool))
    url = SimpleNamespace(path="/api/auth/api-key-session")
    return SimpleNamespace(
        url=url,
        app=app,
        client=SimpleNamespace(host="127.0.0.1"),
        cookies={},
        state=SimpleNamespace(),
        headers={"user-agent": "pytest", "X-API-Key": _KEY},
    )


def _audit_capture(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    async def fake_log_audit(pool, **kw):  # noqa: ARG001
        calls.append(kw)

    monkeypatch.setattr(auth_router, "log_audit", fake_log_audit)
    return calls


@pytest.fixture(autouse=True)
def _set_key(monkeypatch):
    monkeypatch.setenv("DEV_MODE", "true")
    monkeypatch.setenv("JARVIS_API_KEY", _KEY)
    refresh_api_key_cache()
    yield
    monkeypatch.delenv("JARVIS_API_KEY", raising=False)
    refresh_api_key_cache()


async def _call(body: auth_router.ApiKeySessionBody, request, response):
    return await auth_router.api_key_session.__wrapped__(body, request, response)


# (a) valid key + single admin → 200, cookie, admin UserResponse + audit
@pytest.mark.asyncio
async def test_valid_key_single_admin_mints_session(monkeypatch) -> None:
    conn = AsyncMock()
    conn.fetchval = AsyncMock(side_effect=[1, "sess-uuid-1"])  # user_count, session id
    conn.fetchrow = AsyncMock(return_value={"id": 7, "email": "owner@example.com", "role": "admin"})
    pool = _build_mock_pool(conn)
    request = _build_request(pool)
    response = Response()
    audit = _audit_capture(monkeypatch)

    result = await _call(auth_router.ApiKeySessionBody(api_key=_KEY), request, response)

    assert result.id == 7
    assert result.email == "owner@example.com"
    assert result.role == "admin"
    set_cookie = response.headers.get("set-cookie", "")
    assert "jarvis_session=sess-uuid-1" in set_cookie
    assert "HttpOnly" in set_cookie
    assert any(c["action"] == "auth.api_key.session.minted" for c in audit)
    minted = next(c for c in audit if c["action"] == "auth.api_key.session.minted")
    assert minted["user_id"] == "7"


# (b) wrong key → 403, no cookie, failure audit
@pytest.mark.asyncio
async def test_wrong_key_rejected_with_failure_audit(monkeypatch) -> None:
    conn = AsyncMock()
    pool = _build_mock_pool(conn)
    request = _build_request(pool)
    request.headers["X-API-Key"] = "wrong"
    response = Response()
    audit = _audit_capture(monkeypatch)

    with pytest.raises(HTTPException) as exc:
        await _call(auth_router.ApiKeySessionBody(api_key="wrong"), request, response)

    assert exc.value.status_code == 403
    assert "set-cookie" not in response.headers
    assert any(c["action"] == "auth.api_key.session.failure" for c in audit)
    conn.fetchval.assert_not_called()


# (c) >1 user and no opt-in → 403 with the multi-tenant message
@pytest.mark.asyncio
async def test_multi_user_no_optin_returns_403(monkeypatch) -> None:
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=3)  # 3 users
    pool = _build_mock_pool(conn)
    request = _build_request(pool)
    response = Response()
    _audit_capture(monkeypatch)

    with pytest.raises(HTTPException) as exc:
        await _call(auth_router.ApiKeySessionBody(api_key=_KEY), request, response)

    assert exc.value.status_code == 403
    assert "magic-link" in exc.value.detail
    assert "set-cookie" not in response.headers


# (c2) >1 user but API_KEY_LOGIN_ENABLED opt-in → mints
@pytest.mark.asyncio
async def test_multi_user_with_optin_mints(monkeypatch) -> None:
    monkeypatch.setenv("API_KEY_LOGIN_ENABLED", "true")
    conn = AsyncMock()
    conn.fetchval = AsyncMock(side_effect=[3, "sess-2"])
    conn.fetchrow = AsyncMock(return_value={"id": 1, "email": "admin@example.com", "role": "admin"})
    pool = _build_mock_pool(conn)
    request = _build_request(pool)
    response = Response()
    _audit_capture(monkeypatch)

    result = await _call(auth_router.ApiKeySessionBody(api_key=_KEY), request, response)
    assert result.id == 1


# (d) no admin user → 409, no user created
@pytest.mark.asyncio
async def test_no_admin_user_clear_error_no_creation(monkeypatch) -> None:
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=1)
    conn.fetchrow = AsyncMock(return_value=None)  # no admin row
    pool = _build_mock_pool(conn)
    request = _build_request(pool)
    response = Response()
    _audit_capture(monkeypatch)

    with pytest.raises(HTTPException) as exc:
        await _call(auth_router.ApiKeySessionBody(api_key=_KEY), request, response)

    assert exc.value.status_code == 409
    assert "admin" in exc.value.detail.lower()
    # No INSERT into users — only the gate count + admin lookup ran.
    executed = [c.args[0] for c in conn.execute.await_args_list]
    assert not any("INSERT INTO users" in s for s in executed)
    assert "set-cookie" not in response.headers


# (d2) OWNER_USER_ID set but missing → 409, no creation
@pytest.mark.asyncio
async def test_owner_user_id_missing_returns_409(monkeypatch) -> None:
    monkeypatch.setenv("OWNER_USER_ID", "99")
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=1)
    conn.fetchrow = AsyncMock(return_value=None)  # owner id 99 not found
    pool = _build_mock_pool(conn)
    request = _build_request(pool)
    response = Response()
    _audit_capture(monkeypatch)

    with pytest.raises(HTTPException) as exc:
        await _call(auth_router.ApiKeySessionBody(api_key=_KEY), request, response)

    assert exc.value.status_code == 409
    assert "OWNER_USER_ID" in exc.value.detail


# (d3) OWNER_USER_ID set and present → that exact user bound
@pytest.mark.asyncio
async def test_owner_user_id_binds_explicit_user(monkeypatch) -> None:
    monkeypatch.setenv("OWNER_USER_ID", "5")
    conn = AsyncMock()
    conn.fetchval = AsyncMock(side_effect=[1, "sess-3"])
    conn.fetchrow = AsyncMock(
        return_value={"id": 5, "email": "explicit@example.com", "role": "admin"}
    )
    pool = _build_mock_pool(conn)
    request = _build_request(pool)
    response = Response()
    _audit_capture(monkeypatch)

    result = await _call(auth_router.ApiKeySessionBody(api_key=_KEY), request, response)
    assert result.id == 5
    # fetchrow was queried by id (OWNER_USER_ID path), not the admin-scan path.
    assert conn.fetchrow.await_args.args[1] == 5
