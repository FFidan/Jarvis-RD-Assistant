"""Offline-tolerant session grace — `_populate_state_from_cookie`.

A `jarvis_session` cookie that expired no more than ``SESSION_GRACE`` ago must
still resolve identity (so reviews queued offline reconcile after a realistic
gap) WITHOUT renewing ``expires_at``. ``revoked_at``/``deleted_at`` still
hard-fail regardless of grace; a non-expired session is unchanged; a session
expired beyond the grace window does not resolve.

Mock pattern mirrors the asyncpg pool/conn shape used elsewhere
(``pool.acquire()`` → async ctx → conn with awaitable ``fetchrow``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from jarvis_common.session_middleware import (
    SESSION_GRACE,
    _populate_state_from_cookie,
)
from jarvis_common.testing import make_pool_and_conn
from starlette.requests import Request

_SESSION_ID = "550e8400-e29b-41d4-a716-446655440000"


def _make_request(pool: object) -> Request:
    """Starlette Request whose ``app.state.db_pool`` is *pool* (read from scope)."""
    app = SimpleNamespace(state=SimpleNamespace(db_pool=pool))
    return Request({"type": "http", "method": "GET", "path": "/", "headers": [], "app": app})


def _row(
    *,
    expires_at: datetime | None,
    revoked_at: datetime | None = None,
    deleted_at: datetime | None = None,
) -> dict:
    return {
        "user_id": 7,
        "expires_at": expires_at,
        "revoked_at": revoked_at,
        "email": "user@example.com",
        "role": "user",
        "deleted_at": deleted_at,
    }


def _now() -> datetime:
    return datetime.now(UTC)


@pytest.mark.asyncio
async def test_grace_constant_is_24h() -> None:
    assert SESSION_GRACE == timedelta(hours=24)


# test_non_expired_session_resolves — deleted; covered by
#   libs/jarvis_common/tests/contract/test_ws_auth_contract.py::test_active_session_resolves_user_id

# test_expired_within_grace_resolves — deleted; covered by
#   libs/jarvis_common/tests/contract/test_ws_auth_contract.py::test_grace_window_session_still_resolves

# test_expired_at_grace_edge_resolves — deleted; covered by
#   libs/jarvis_common/tests/contract/test_ws_auth_contract.py::test_grace_window_session_still_resolves

# test_expired_beyond_grace_does_not_resolve — deleted; covered by
#   libs/jarvis_common/tests/contract/test_ws_auth_contract.py::test_hard_expired_session_does_not_resolve

# test_revoked_within_grace_hard_fails — deleted; covered by
#   libs/jarvis_common/tests/contract/test_ws_auth_contract.py::test_revoked_session_does_not_resolve


@pytest.mark.asyncio
async def test_deleted_user_within_grace_hard_fails() -> None:
    """deleted_at set hard-fails even when expiry is within grace."""
    pool, _ = make_pool_and_conn(
        fetchrow_return=_row(
            expires_at=_now() - timedelta(hours=1), deleted_at=_now() - timedelta(days=1)
        )
    )
    request = _make_request(pool)
    await _populate_state_from_cookie(request, _SESSION_ID)
    assert not hasattr(request.state, "user_id")


@pytest.mark.asyncio
async def test_grace_does_not_renew_expires_at() -> None:
    """Grace resolves identity but performs no write (no UPDATE/execute)."""
    pool, conn = make_pool_and_conn(fetchrow_return=_row(expires_at=_now() - timedelta(hours=1)))
    request = _make_request(pool)
    await _populate_state_from_cookie(request, _SESSION_ID)
    assert request.state.user_id == 7
    conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_null_expiry_resolves() -> None:
    """A session with NULL expires_at is non-expiring — still resolves."""
    pool, _ = make_pool_and_conn(fetchrow_return=_row(expires_at=None))
    request = _make_request(pool)
    await _populate_state_from_cookie(request, _SESSION_ID)
    assert request.state.user_id == 7
