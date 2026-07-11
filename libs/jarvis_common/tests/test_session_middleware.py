"""Unit tests for jarvis_common.session_middleware."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from jarvis_common.session_middleware import (
    SESSION_COOKIE_NAME,
    SessionMiddleware,
    _populate_state_from_cookie,
)
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
    conn.fetchrow = AsyncMock(return_value=row)
    pool.acquire = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    await _populate_state_from_cookie(request, "session-id")

    # request.state.user_id SHOULD be set
    assert request.state.user_id == 42
    assert request.state.user_email == "test@example.com"
    assert request.state.user_role == "user"


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
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=row)
    conn.fetchval = AsyncMock(return_value=renewed)
    pool.acquire = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return conn


@pytest.mark.asyncio
async def test_active_session_renews_and_marks_state(mock_pool):
    """An active, renewable session runs the UPDATE and flags session_renewed."""
    pool, request = mock_pool
    future = datetime.now(UTC) + timedelta(days=20)
    conn = _wire_conn(pool, row=_row(future), renewed="session-id")

    await _populate_state_from_cookie(request, "session-id")

    assert request.state.user_id == 42
    conn.fetchval.assert_awaited_once()
    assert request.state.session_renewed == "session-id"


@pytest.mark.asyncio
async def test_grace_expired_resolves_but_does_not_renew(mock_pool):
    """A grace-expired session resolves identity but the predicate blocks renewal."""
    pool, request = mock_pool
    expired = datetime.now(UTC) - timedelta(hours=12)  # within SESSION_GRACE
    _wire_conn(pool, row=_row(expired), renewed=None)  # predicate matches no row

    await _populate_state_from_cookie(request, "session-id")

    assert request.state.user_id == 42
    assert not hasattr(request.state, "session_renewed")


@pytest.mark.asyncio
async def test_recently_renewed_session_is_throttled(mock_pool):
    """A session already close to full TTL (renewed today) is not written again."""
    pool, request = mock_pool
    near_full = datetime.now(UTC) + timedelta(days=30) - timedelta(hours=1)
    conn = _wire_conn(pool, row=_row(near_full), renewed=None)  # throttle → no row

    await _populate_state_from_cookie(request, "session-id")

    assert request.state.user_id == 42
    conn.fetchval.assert_awaited_once()
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


@pytest.mark.asyncio
async def test_dispatch_refreshes_cookie_when_session_renewed(mock_pool):
    """dispatch re-issues the session cookie (same id, 30-day Max-Age) after renewal."""
    pool, request = mock_pool
    request.cookies = {SESSION_COOKIE_NAME: "session-id"}
    future = datetime.now(UTC) + timedelta(days=20)
    _wire_conn(pool, row=_row(future), renewed="session-id")

    response = Response()
    call_next = AsyncMock(return_value=response)
    middleware = SessionMiddleware(MagicMock())

    result = await middleware.dispatch(request, call_next)

    set_cookie = result.headers.get("set-cookie")
    assert set_cookie is not None
    assert f"{SESSION_COOKIE_NAME}=session-id" in set_cookie
    assert "Max-Age=2592000" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=strict" in set_cookie


@pytest.mark.asyncio
async def test_dispatch_leaves_cookie_untouched_when_not_renewed(mock_pool):
    """dispatch sets no cookie when the session was resolved but not renewed."""
    pool, request = mock_pool
    request.cookies = {SESSION_COOKIE_NAME: "session-id"}
    near_full = datetime.now(UTC) + timedelta(days=30) - timedelta(hours=1)
    _wire_conn(pool, row=_row(near_full), renewed=None)

    response = Response()
    call_next = AsyncMock(return_value=response)
    middleware = SessionMiddleware(MagicMock())

    result = await middleware.dispatch(request, call_next)

    assert result.headers.get("set-cookie") is None


__all__ = []
