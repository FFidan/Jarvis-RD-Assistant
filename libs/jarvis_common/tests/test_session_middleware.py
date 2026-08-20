"""Unit tests for jarvis_common.session_middleware."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jarvis_common.session_middleware import (
    SESSION_COOKIE_NAME,
    SESSION_TTL,
    SessionMiddleware,
    _populate_state_from_cookie,
    session_cookie_kwargs,
)
from jarvis_common.testing import make_pool_and_conn
from starlette.responses import Response


class MockState:
    """A state object that tracks attributes set on it."""

    def __init__(self):
        self._attrs = {}

    def __setattr__(self, name, value):
        if name.startswith("_"):
            object.__setattr__(self, name, value)
        else:
            self._attrs[name] = value

    def __getattr__(self, name):
        if name in self._attrs:
            return self._attrs[name]
        raise AttributeError(f"MockState has no attribute {name}")

    def __hasattr__(self, name):
        return name in self._attrs


@pytest.fixture
def mock_request():
    """Create a mock request with proper state isolation."""
    request = MagicMock()
    request.state = MockState()
    request.cookies = {}
    request.app.state.db_pool = AsyncMock()
    return request


@pytest.fixture
def mock_pool(mock_request):
    """Create a mock asyncpg pool."""
    pool = AsyncMock()
    mock_request.app.state.db_pool = pool
    return pool, mock_request


def _enable_transactions(conn: AsyncMock) -> None:
    """Configure an async context manager for a mocked connection savepoint."""
    conn.transaction = MagicMock()
    conn.transaction.return_value.__aenter__ = AsyncMock(return_value=None)
    conn.transaction.return_value.__aexit__ = AsyncMock(return_value=False)


# ---------------------------------------------------------------------------
# expires_at IS NOT NULL defense-in-depth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_populate_state_rejects_null_expires_at(mock_pool):
    """_populate_state_from_cookie rejects sessions with NULL expires_at.

    Even though the schema enforces NOT NULL and the SQL has `AND expires_at IS NOT NULL`,
    the Python code adds defense-in-depth by explicitly checking for NULL.

    Grounding: session_middleware.py:100-101.
    """
    pool, request = mock_pool

    # Simulate a session row with NULL expires_at (impossible via schema, but we
    # test the Python-level defense-in-depth check).
    row = {
        "user_id": 42,
        "expires_at": None,  # The problematic NULL
        "revoked_at": None,
        "email": "test@example.com",
        "role": "user",
        "deleted_at": None,
    }

    conn = AsyncMock()
    _enable_transactions(conn)
    conn.fetchrow = AsyncMock(return_value=row)
    pool.acquire = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    await _populate_state_from_cookie(request, "session-id")

    # request.state.user_id should NOT be set because expires_at is NULL
    assert not hasattr(request.state, "user_id"), (
        "SessionMiddleware should reject sessions with NULL expires_at"
    )


@pytest.mark.asyncio
async def test_populate_state_accepts_valid_session(mock_pool):
    """_populate_state_from_cookie accepts a session with valid expires_at.

    Grounding: session_middleware.py:107-109.
    """
    pool, request = mock_pool

    future = datetime.now(UTC) + timedelta(hours=1)
    row = {
        "user_id": 42,
        "expires_at": future,
        "revoked_at": None,
        "email": "test@example.com",
        "role": "user",
        "deleted_at": None,
    }

    conn = AsyncMock()
    _enable_transactions(conn)
    conn.fetchrow = AsyncMock(return_value=row)
    pool.acquire = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    await _populate_state_from_cookie(request, "session-id")

    # request.state.user_id SHOULD be set
    assert request.state.user_id == 42
    assert request.state.user_email == "test@example.com"
    assert request.state.user_role == "user"
    assert request.state.session_id == "session-id"


@pytest.mark.asyncio
async def test_populate_state_accepts_expired_within_grace(mock_pool):
    """_populate_state_from_cookie accepts sessions expired within the grace window.

    Grounding: session_middleware.py:99 (grace window comment) and 103-105.
    """
    pool, request = mock_pool

    # Expired 12 hours ago (within 24-hour grace)
    expired = datetime.now(UTC) - timedelta(hours=12)
    row = {
        "user_id": 42,
        "expires_at": expired,
        "revoked_at": None,
        "email": "test@example.com",
        "role": "user",
        "deleted_at": None,
    }

    conn = AsyncMock()
    _enable_transactions(conn)
    conn.fetchrow = AsyncMock(return_value=row)
    pool.acquire = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    await _populate_state_from_cookie(request, "session-id")

    # Should accept because it's within grace
    assert request.state.user_id == 42


@pytest.mark.asyncio
async def test_populate_state_rejects_expired_outside_grace(mock_pool):
    """_populate_state_from_cookie rejects sessions expired outside the grace window.

    Grounding: session_middleware.py:103-105.
    """
    pool, request = mock_pool

    # Expired 30 hours ago (outside 24-hour grace)
    expired = datetime.now(UTC) - timedelta(hours=30)
    row = {
        "user_id": 42,
        "expires_at": expired,
        "revoked_at": None,
        "email": "test@example.com",
        "role": "user",
        "deleted_at": None,
    }

    conn = AsyncMock()
    _enable_transactions(conn)
    conn.fetchrow = AsyncMock(return_value=row)
    pool.acquire = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    await _populate_state_from_cookie(request, "session-id")

    # Should reject because it's outside grace
    assert not hasattr(request.state, "user_id")


# ---------------------------------------------------------------------------
# Rolling renewal — DB row (_populate_state_from_cookie) and cookie (dispatch)
# ---------------------------------------------------------------------------


def _row(expires_at, *, revoked_at=None, deleted_at=None):
    """Build a session-lookup row dict with sensible identity defaults."""
    return {
        "user_id": 42,
        "expires_at": expires_at,
        "revoked_at": revoked_at,
        "email": "test@example.com",
        "role": "user",
        "deleted_at": deleted_at,
    }


def _wire_conn(pool, *, row, renewed=None):
    """Configure the mock pool's acquired connection for lookup + renewal.

    ``row`` is what the session lookup ``fetchrow`` returns; ``renewed`` is the id
    the atomic renewal UPDATE ``fetchval`` returns (None ⇒ the predicate — grace /
    revoked / throttle — matched no row). Both acquires yield this same conn.
    """
    wired, conn = make_pool_and_conn(fetchrow_return=row, fetchval_return=renewed)
    # The pool under test comes from the fixture; graft the wired acquire onto it.
    pool.acquire = wired.acquire
    return conn


@pytest.mark.asyncio
async def test_active_session_renews_and_marks_state(mock_pool):
    """An active session resolves and renews through one acquired connection."""
    pool, request = mock_pool
    future = datetime.now(UTC) + timedelta(days=20)
    conn = _wire_conn(pool, row=_row(future), renewed="session-id")

    await _populate_state_from_cookie(request, "session-id")

    assert request.state.user_id == 42
    assert pool.acquire.call_count == 1
    conn.fetchval.assert_awaited_once()
    assert request.state.session_renewed == "session-id"


@pytest.mark.asyncio
async def test_concurrent_requests_share_only_the_overlapping_session_lookup(mock_pool):
    """Concurrent requests share one lookup without retaining a completed result."""
    pool, request = mock_pool
    near_full = datetime.now(UTC) + timedelta(days=30) - timedelta(hours=1)
    conn = _wire_conn(pool, row=_row(near_full), renewed=None)
    requests = [request]
    for _ in range(9):
        peer = MagicMock()
        peer.state = MockState()
        peer.cookies = {}
        peer.app.state.db_pool = pool
        requests.append(peer)

    await asyncio.gather(*(_populate_state_from_cookie(peer, "session-id") for peer in requests))

    assert pool.acquire.call_count == 1
    conn.fetchrow.assert_awaited_once()
    assert all(peer.state.user_id == 42 for peer in requests)

    later = MagicMock()
    later.state = MockState()
    later.cookies = {}
    later.app.state.db_pool = pool
    await _populate_state_from_cookie(later, "session-id")
    assert pool.acquire.call_count == 2


@pytest.mark.asyncio
async def test_grace_expired_resolves_but_does_not_renew(mock_pool):
    """A grace-expired session resolves identity without a renewal query."""
    pool, request = mock_pool
    expired = datetime.now(UTC) - timedelta(hours=12)  # within SESSION_GRACE
    conn = _wire_conn(pool, row=_row(expired), renewed=None)

    await _populate_state_from_cookie(request, "session-id")

    assert request.state.user_id == 42
    conn.fetchval.assert_not_awaited()
    assert not hasattr(request.state, "session_renewed")


@pytest.mark.asyncio
async def test_recently_renewed_session_is_throttled(mock_pool):
    """A recently renewed session skips the database renewal query."""
    pool, request = mock_pool
    near_full = datetime.now(UTC) + timedelta(days=30) - timedelta(hours=1)
    conn = _wire_conn(pool, row=_row(near_full), renewed=None)

    await _populate_state_from_cookie(request, "session-id")

    assert request.state.user_id == 42
    conn.fetchval.assert_not_awaited()
    assert not hasattr(request.state, "session_renewed")


@pytest.mark.asyncio
async def test_revoked_session_never_resolves_or_renews(mock_pool):
    """A revoked session resolves no identity and the renewal UPDATE never runs."""
    pool, request = mock_pool
    future = datetime.now(UTC) + timedelta(days=10)
    conn = _wire_conn(pool, row=_row(future, revoked_at=datetime.now(UTC)))

    await _populate_state_from_cookie(request, "session-id")

    assert not hasattr(request.state, "user_id")
    conn.fetchval.assert_not_awaited()
    assert not hasattr(request.state, "session_renewed")


def _session_test_client(pool: AsyncMock) -> TestClient:
    """Return a real ASGI client backed by the supplied session pool."""
    app = FastAPI()
    app.state.db_pool = pool
    app.add_middleware(SessionMiddleware)

    @app.get("/ping")
    async def ping() -> Response:
        return Response(status_code=204)

    return TestClient(app)


def test_middleware_refreshes_cookie_when_session_renewed(mock_pool):
    """The ASGI middleware reissues a renewed session cookie."""
    pool, _ = mock_pool
    future = datetime.now(UTC) + timedelta(days=20)
    _wire_conn(pool, row=_row(future), renewed="session-id")

    client = _session_test_client(pool)
    client.cookies.set(SESSION_COOKIE_NAME, "session-id")
    result = client.get("/ping")

    set_cookie = result.headers.get("set-cookie")
    assert set_cookie is not None
    assert f"{SESSION_COOKIE_NAME}=session-id" in set_cookie
    assert "Max-Age=2592000" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=strict" in set_cookie
    # the absolute Expires must be ~now+30d, not the ~year-2083 artifact of
    # passing an epoch int (which starlette misreads as delta-seconds).
    from email.utils import parsedate_to_datetime

    expires_str = next(
        part.split("=", 1)[1]
        for part in set_cookie.split("; ")
        if part.lower().startswith("expires=")
    )
    parsed = parsedate_to_datetime(expires_str)
    assert abs((parsed - (datetime.now(UTC) + SESSION_TTL)).total_seconds()) <= 2 * 86400, (
        f"renewal Expires must be ~now+30d; got {parsed.isoformat()} from {expires_str!r}"
    )


def test_middleware_leaves_cookie_untouched_when_not_renewed(mock_pool):
    """The ASGI middleware sets no cookie for a recently renewed session."""
    pool, _ = mock_pool
    near_full = datetime.now(UTC) + timedelta(days=30) - timedelta(hours=1)
    _wire_conn(pool, row=_row(near_full), renewed=None)

    client = _session_test_client(pool)
    client.cookies.set(SESSION_COOKIE_NAME, "session-id")
    result = client.get("/ping")

    assert result.headers.get("set-cookie") is None


# ---------------------------------------------------------------------------
# cookie Expires is an absolute now+TTL date, not a ~2083 artifact
# ---------------------------------------------------------------------------


def test_session_cookie_kwargs_expires_matches_max_age():
    """session_cookie_kwargs emits an absolute Expires ~now+TTL.

    The bug: passing ``int(...timestamp())`` handed starlette a non-datetime
    ``expires``, which ``http.cookies._getdate`` reads as delta-seconds → an
    Expires ~60 years out. The fix passes an aware-UTC datetime so starlette
    takes the ``format_datetime(usegmt=True)`` branch. ``Max-Age`` is unaffected.
    """
    from email.utils import parsedate_to_datetime

    fixed_now = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
    max_age = int(SESSION_TTL.total_seconds())

    response = Response()
    response.set_cookie(
        SESSION_COOKIE_NAME,
        "session-id",
        **session_cookie_kwargs(max_age, now=fixed_now),
    )
    set_cookie = response.headers.get("set-cookie")
    assert set_cookie is not None
    assert "Max-Age=2592000" in set_cookie

    expires_str = next(
        part.split("=", 1)[1]
        for part in set_cookie.split("; ")
        if part.lower().startswith("expires=")
    )
    parsed = parsedate_to_datetime(expires_str)
    assert abs((parsed - (fixed_now + SESSION_TTL)).total_seconds()) <= 2 * 86400, (
        f"Expires must be ~fixed_now+30d, not ~2083; got {parsed.isoformat()} from {expires_str!r}"
    )


__all__ = []


def _signing_out_test_client(pool: AsyncMock) -> TestClient:
    """Return a client whose route clears the session cookie, as sign-out does."""
    app = FastAPI()
    app.state.db_pool = pool
    app.add_middleware(SessionMiddleware)

    @app.post("/sign-out")
    async def sign_out() -> Response:
        response = Response(status_code=204)
        response.delete_cookie(SESSION_COOKIE_NAME, path="/")
        return response

    return TestClient(app)


def test_middleware_does_not_renew_a_session_the_handler_cleared(mock_pool):
    """Signing out ends the session even when the request was renewal-eligible.

    The rolling refresh is attached to responses that arrived with a live
    session, which is exactly the state a sign-out request is in. If it were
    appended here it would be the last Set-Cookie the browser saw, and the
    session would survive the sign-out.
    """
    pool, _ = mock_pool
    future = datetime.now(UTC) + timedelta(days=20)
    _wire_conn(pool, row=_row(future), renewed="session-id")

    client = _signing_out_test_client(pool)
    client.cookies.set(SESSION_COOKIE_NAME, "session-id")
    result = client.post("/sign-out")

    session_cookies = [
        value
        for key, value in result.headers.items()
        if key.lower() == "set-cookie" and value.startswith(f"{SESSION_COOKIE_NAME}=")
    ]
    assert session_cookies, "sign-out must emit a session cookie header"
    assert not any("Max-Age=2592000" in cookie for cookie in session_cookies), (
        f"sign-out must not be handed a renewed session cookie; got {session_cookies}"
    )
