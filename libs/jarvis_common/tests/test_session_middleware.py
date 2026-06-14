"""Unit tests for jarvis_common.session_middleware."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from jarvis_common.session_middleware import (
    _populate_state_from_cookie,
)


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


__all__ = []
