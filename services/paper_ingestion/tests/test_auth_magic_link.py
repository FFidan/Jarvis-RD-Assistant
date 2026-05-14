"""Phase 2 WS-2A: magic-link auth + session middleware unit tests.

Mocks the DB pool so the suite runs without Docker. The full integration
flow (request → verify → access protected route → logout) is exercised in
``test_auth_magic_link_integration.py`` against the live PG fixture.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import paper_ingestion.routers.auth as auth_router
import pytest
from fastapi import HTTPException, Response
from jarvis_common.auth import current_user_id_or_none


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Pool fixture: mocks pool.acquire() context manager + conn.transaction()
# ---------------------------------------------------------------------------


def _build_mock_pool(conn: AsyncMock) -> MagicMock:
    """Wrap an AsyncMock conn in a pool whose acquire() yields it."""
    txn = MagicMock()
    txn.__aenter__ = AsyncMock(return_value=txn)
    txn.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn)

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)

    pool = MagicMock()
    pool.acquire.return_value = ctx
    return pool


def _build_request(pool: MagicMock, *, cookies: dict[str, str] | None = None) -> SimpleNamespace:
    """Build a Request stub good enough for the auth router and middleware."""
    state = SimpleNamespace(db_pool=pool)
    app = SimpleNamespace(state=state)
    url = SimpleNamespace(
        path="/api/auth/request-link",
        replace=lambda **kw: SimpleNamespace(__str__=lambda self: "https://x/auth/verify?token=t"),
    )
    return SimpleNamespace(
        url=url,
        app=app,
        client=SimpleNamespace(host="127.0.0.1"),
        cookies=cookies or {},
        state=SimpleNamespace(),
    )


# ---------------------------------------------------------------------------
# request-link
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_link_unknown_email_returns_sent_true(monkeypatch) -> None:
    """Unknown emails get the same response shape (no enumeration leak)."""
    monkeypatch.setenv("DEV_MODE", "true")
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    pool = _build_mock_pool(conn)
    request = _build_request(pool)

    result = await auth_router.request_link.__wrapped__(
        auth_router.RequestLinkBody(email="ghost@example.com"),
        request,
    )

    assert result.sent is True
    # No magic_link_tokens INSERT should have run
    conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_request_link_known_email_inserts_token_and_logs(monkeypatch) -> None:
    """Known email → token row inserted, dev-mode log emitted."""
    monkeypatch.setenv("DEV_MODE", "true")
    monkeypatch.setenv("APP_BASE_URL", "https://localhost:3001")
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"id": 42})
    conn.execute = AsyncMock()
    pool = _build_mock_pool(conn)
    request = _build_request(pool)

    sent_calls: list[tuple[str, str]] = []

    async def fake_send_magic_link(email, link, *, pool=None):
        sent_calls.append((email, link))

    monkeypatch.setattr(auth_router, "send_magic_link", fake_send_magic_link)

    result = await auth_router.request_link.__wrapped__(
        auth_router.RequestLinkBody(email="ferhat@example.com"),
        request,
    )

    assert result.sent is True
    conn.execute.assert_awaited_once()
    insert_sql = conn.execute.await_args.args[0]
    assert "INSERT INTO magic_link_tokens" in insert_sql
    assert len(sent_calls) == 1
    assert sent_calls[0][0] == "ferhat@example.com"
    assert "token=" in sent_calls[0][1]


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_unknown_token_rejected(monkeypatch) -> None:
    monkeypatch.setenv("DEV_MODE", "true")
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    pool = _build_mock_pool(conn)
    request = _build_request(pool)

    with pytest.raises(HTTPException) as exc:
        await auth_router.verify.__wrapped__(
            auth_router.VerifyBody(token="A" * 32),
            request,
            Response(),
        )
    assert exc.value.status_code == 400
    assert exc.value.detail == "Invalid or expired token"


@pytest.mark.asyncio
async def test_verify_expired_token_rejected(monkeypatch) -> None:
    monkeypatch.setenv("DEV_MODE", "true")
    conn = AsyncMock()
    expired_at = datetime.now(UTC) - timedelta(minutes=1)
    conn.fetchrow = AsyncMock(
        return_value={
            "user_id": 7,
            "expires_at": expired_at,
            "used_at": None,
        }
    )
    pool = _build_mock_pool(conn)
    request = _build_request(pool)

    with pytest.raises(HTTPException) as exc:
        await auth_router.verify.__wrapped__(
            auth_router.VerifyBody(token="A" * 32),
            request,
            Response(),
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_verify_reused_token_rejected(monkeypatch) -> None:
    monkeypatch.setenv("DEV_MODE", "true")
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        return_value={
            "user_id": 7,
            "expires_at": datetime.now(UTC) + timedelta(minutes=10),
            "used_at": datetime.now(UTC) - timedelta(seconds=10),  # already used
        }
    )
    pool = _build_mock_pool(conn)
    request = _build_request(pool)

    with pytest.raises(HTTPException) as exc:
        await auth_router.verify.__wrapped__(
            auth_router.VerifyBody(token="A" * 32),
            request,
            Response(),
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_verify_happy_path_sets_cookie_and_returns_user(monkeypatch) -> None:
    """Valid token: token marked used, session created, cookie set, user returned."""
    monkeypatch.setenv("DEV_MODE", "true")
    conn = AsyncMock()
    # Sequence of fetchrow calls: token lookup → user lookup
    conn.fetchrow = AsyncMock(
        side_effect=[
            {
                "user_id": 7,
                "expires_at": datetime.now(UTC) + timedelta(minutes=10),
                "used_at": None,
            },
            {"id": 7, "email": "ferhat@example.com", "role": "admin", "deleted_at": None},
        ]
    )
    conn.execute = AsyncMock()
    conn.fetchval = AsyncMock(return_value="00000000-0000-0000-0000-000000000099")
    pool = _build_mock_pool(conn)
    request = _build_request(pool)
    response = Response()

    user = await auth_router.verify.__wrapped__(
        auth_router.VerifyBody(token="A" * 32),
        request,
        response,
    )

    assert user.id == 7
    assert user.email == "ferhat@example.com"
    assert user.role == "admin"
    # Cookie should be set on the response
    set_cookie_headers = [v for k, v in response.raw_headers if k.lower() == b"set-cookie"]
    assert any(b"jarvis_session=" in h for h in set_cookie_headers)
    assert any(b"HttpOnly" in h for h in set_cookie_headers)
    assert any(
        b"SameSite=strict" in h.lower() or b"samesite=strict" in h.lower()
        for h in set_cookie_headers
    )
    # In DEV_MODE Secure must NOT be set
    assert not any(b"Secure" in h for h in set_cookie_headers)


@pytest.mark.asyncio
async def test_verify_secure_cookie_in_prod_mode(monkeypatch) -> None:
    monkeypatch.setenv("DEV_MODE", "false")
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            {
                "user_id": 7,
                "expires_at": datetime.now(UTC) + timedelta(minutes=10),
                "used_at": None,
            },
            {"id": 7, "email": "u@example.com", "role": "user", "deleted_at": None},
        ]
    )
    conn.execute = AsyncMock()
    conn.fetchval = AsyncMock(return_value="00000000-0000-0000-0000-000000000099")
    pool = _build_mock_pool(conn)
    request = _build_request(pool)
    response = Response()

    await auth_router.verify.__wrapped__(
        auth_router.VerifyBody(token="A" * 32),
        request,
        response,
    )

    set_cookie_headers = [v for k, v in response.raw_headers if k.lower() == b"set-cookie"]
    assert any(b"Secure" in h for h in set_cookie_headers)


# ---------------------------------------------------------------------------
# logout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_logout_revokes_session_and_clears_cookie(monkeypatch) -> None:
    monkeypatch.setenv("DEV_MODE", "true")
    conn = AsyncMock()
    conn.execute = AsyncMock()
    pool = _build_mock_pool(conn)
    request = _build_request(
        pool, cookies={"jarvis_session": "00000000-0000-0000-0000-000000000099"}
    )
    response = Response()

    await auth_router.logout(request, response)

    conn.execute.assert_awaited_once()
    update_sql = conn.execute.await_args.args[0]
    assert "UPDATE sessions SET revoked_at" in update_sql
    set_cookie_headers = [v for k, v in response.raw_headers if k.lower() == b"set-cookie"]
    assert any(b"jarvis_session=" in h for h in set_cookie_headers)


@pytest.mark.asyncio
async def test_logout_with_no_cookie_is_noop(monkeypatch) -> None:
    monkeypatch.setenv("DEV_MODE", "true")
    conn = AsyncMock()
    conn.execute = AsyncMock()
    pool = _build_mock_pool(conn)
    request = _build_request(pool, cookies={})
    response = Response()

    await auth_router.logout(request, response)

    conn.execute.assert_not_called()


# ---------------------------------------------------------------------------
# session middleware + current_user_id_or_none
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_middleware_populates_state_for_valid_session() -> None:
    """SessionMiddleware._populate_state_from_cookie sets request.state.user_id."""
    from jarvis_common.session_middleware import _populate_state_from_cookie

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        return_value={
            "user_id": 7,
            "expires_at": datetime.now(UTC) + timedelta(days=1),
            "revoked_at": None,
            "email": "ferhat@example.com",
            "role": "admin",
            "deleted_at": None,
        }
    )
    pool = _build_mock_pool(conn)
    request = _build_request(pool)

    await _populate_state_from_cookie(request, "00000000-0000-0000-0000-000000000099")

    assert request.state.user_id == 7
    assert request.state.user_email == "ferhat@example.com"
    assert request.state.user_role == "admin"


@pytest.mark.asyncio
async def test_session_middleware_skips_revoked_session() -> None:
    from jarvis_common.session_middleware import _populate_state_from_cookie

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        return_value={
            "user_id": 7,
            "expires_at": datetime.now(UTC) + timedelta(days=1),
            "revoked_at": datetime.now(UTC),
            "email": "x@example.com",
            "role": "user",
            "deleted_at": None,
        }
    )
    pool = _build_mock_pool(conn)
    request = _build_request(pool)

    await _populate_state_from_cookie(request, "00000000-0000-0000-0000-000000000099")

    assert not hasattr(request.state, "user_id")


@pytest.mark.asyncio
async def test_session_middleware_skips_expired_session() -> None:
    from jarvis_common.session_middleware import _populate_state_from_cookie

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        return_value={
            "user_id": 7,
            "expires_at": datetime.now(UTC) - timedelta(days=1),
            "revoked_at": None,
            "email": "x@example.com",
            "role": "user",
            "deleted_at": None,
        }
    )
    pool = _build_mock_pool(conn)
    request = _build_request(pool)

    await _populate_state_from_cookie(request, "00000000-0000-0000-0000-000000000099")

    assert not hasattr(request.state, "user_id")


@pytest.mark.asyncio
async def test_current_user_id_or_none_reads_request_state() -> None:
    """The shared helper reads request.state.user_id when present."""
    request = SimpleNamespace(state=SimpleNamespace(user_id=42))
    assert await current_user_id_or_none(request) == 42


@pytest.mark.asyncio
async def test_current_user_id_or_none_returns_none_when_state_unset() -> None:
    request = SimpleNamespace(state=SimpleNamespace())
    assert await current_user_id_or_none(request) is None


# ---------------------------------------------------------------------------
# dev-mode email fallback emits system_events row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_magic_link_dev_mode_emits_system_event(monkeypatch) -> None:
    """When DEV_MODE=true, send_magic_link logs + writes to system_events."""
    from jarvis_common import email as email_module

    monkeypatch.setenv("DEV_MODE", "true")
    log_event_calls: list[dict] = []

    async def fake_log_event(**kwargs):
        log_event_calls.append(kwargs)

    monkeypatch.setattr(email_module, "log_event", fake_log_event)

    fake_pool = object()
    await email_module.send_magic_link(
        "ferhat@example.com",
        "https://localhost:3001/auth/verify?token=abc",
        pool=fake_pool,
    )

    assert len(log_event_calls) == 1
    call = log_event_calls[0]
    assert call["category"] == "auth"
    assert call["source"] == "auth"
    assert call["message"] == "magic_link_dev_mode"
    assert call["context"]["email"] == "ferhat@example.com"
    assert "token=abc" in call["context"]["link"]


@pytest.mark.asyncio
async def test_send_magic_link_dev_mode_no_pool_still_logs(monkeypatch) -> None:
    """Pool=None is allowed; logger.info still runs without DB."""
    from jarvis_common import email as email_module

    monkeypatch.setenv("DEV_MODE", "true")
    monkeypatch.delenv("SMTP_HOST", raising=False)

    # Should not raise even without a pool
    await email_module.send_magic_link("x@example.com", "https://x/v?token=t")


# ---------------------------------------------------------------------------
# token hashing
# ---------------------------------------------------------------------------


def test_token_hash_is_sha256_hex() -> None:
    """Internal hash function uses canonical SHA-256 hex form."""
    raw = "test-token"
    assert auth_router._hash_token(raw) == _hash(raw)
    assert len(auth_router._hash_token(raw)) == 64  # 32 bytes hex


# ---------------------------------------------------------------------------
# Per-endpoint rate limits (H15)
# ---------------------------------------------------------------------------


@pytest.fixture()
def _rate_limit_client(monkeypatch):
    """TestClient with rate limiter *enabled* and in-memory storage reset each test."""
    monkeypatch.setenv("DEV_MODE", "true")
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost/test")
    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")

    from fastapi.testclient import TestClient
    from jarvis_common import verify_api_key
    from paper_ingestion.main import app

    app.dependency_overrides[verify_api_key] = lambda: None
    # Enable the limiter and reset any counters left by previous tests
    app.state.limiter.enabled = True
    app.state.limiter._storage.reset()

    client = TestClient(app, raise_server_exceptions=False)

    yield client

    app.state.limiter._storage.reset()
    app.state.limiter.enabled = False
    app.dependency_overrides.clear()


def test_request_link_rate_limited(_rate_limit_client) -> None:
    """6th POST /api/auth/request-link from the same IP returns 429."""
    client = _rate_limit_client
    payload = {"email": "ratelimit@example.com"}

    for i in range(5):
        resp = client.post("/api/auth/request-link", json=payload)
        assert resp.status_code != 429, f"Unexpectedly rate-limited on call {i + 1}"

    resp = client.post("/api/auth/request-link", json=payload)
    assert resp.status_code == 429, f"Expected 429 on 6th call, got {resp.status_code}"


def test_verify_rate_limited(_rate_limit_client) -> None:
    """11th POST /api/auth/verify from the same IP returns 429."""
    client = _rate_limit_client
    payload = {"token": "A" * 32}

    for i in range(10):
        resp = client.post("/api/auth/verify", json=payload)
        assert resp.status_code != 429, f"Unexpectedly rate-limited on call {i + 1}"

    resp = client.post("/api/auth/verify", json=payload)
    assert resp.status_code == 429, f"Expected 429 on 11th call, got {resp.status_code}"
