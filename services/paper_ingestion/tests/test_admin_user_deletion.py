"""User-deletion: restore endpoint + admin audit-log coverage.

Reuses the mocked-pool style from test_admin_users.py (no Docker needed).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import platform_api.routers.admin as admin_router
import pytest
from fastapi import HTTPException
from jarvis_common.owner import OwnerIdentity

from tests._auth_fakes import (
    build_mock_pool,
    build_mock_pool_with_txn,
    build_request_admin,
)

# Fixed stand-in for "now": row timestamps must not depend on when the suite runs.
_FIXED_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

# Pool/request stubs delegated to shared _auth_fakes (D5-03).
_build_mock_pool = build_mock_pool
_build_mock_pool_txn = build_mock_pool_with_txn
_build_request = build_request_admin


@pytest.fixture(autouse=True)
def _missing_owner_configuration(monkeypatch):
    monkeypatch.setattr(
        admin_router,
        "resolve_owner_identity",
        AsyncMock(return_value=OwnerIdentity(source="none", state="missing")),
    )


def _user_row(*, id=2, email="a@x.com", role="user") -> dict:
    return {
        "id": id,
        "email": email,
        "role": role,
        "created_at": _FIXED_NOW - timedelta(days=1),
        "last_login_at": None,
    }


# --------------------------------------------------------------------------
# restore endpoint
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restore_clears_deleted_at_within_grace(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_MODEL_HMAC_KEY", "x" * 32)  # multi-user signing key required
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            {**_user_row(id=5), "deleted_at": _FIXED_NOW},
            _user_row(id=5),
        ]
    )
    pool = _build_mock_pool_txn(conn)
    request = _build_request(pool, user_id=1, user_role="admin")

    result = await admin_router.restore_user(5, request)

    assert result.id == 5
    select_sql = conn.fetchrow.await_args_list[0].args[0]
    update_sql = conn.fetchrow.await_args_list[1].args[0]
    assert "30 days" in select_sql
    assert "deleted_at = NULL" in update_sql
    assert "deleted_at IS NOT NULL" in update_sql


@pytest.mark.asyncio
async def test_restore_not_found_or_past_grace_raises_404(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_MODEL_HMAC_KEY", "x" * 32)  # multi-user signing key required
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    pool = _build_mock_pool_txn(conn)
    request = _build_request(pool, user_id=1, user_role="admin")

    with pytest.raises(HTTPException) as exc:
        await admin_router.restore_user(999, request)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_restore_is_admin_only() -> None:
    request = _build_request(_build_mock_pool(AsyncMock()), user_id=2, user_role="user")
    with pytest.raises(HTTPException) as exc:
        await admin_router.require_admin(request)
    assert exc.value.status_code == 403


# --------------------------------------------------------------------------
# audit-log: invite / role_change / soft_delete
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invite_user_writes_audit(monkeypatch) -> None:
    monkeypatch.setenv("DEV_MODE", "true")
    monkeypatch.setenv("JARVIS_MODEL_HMAC_KEY", "x" * 32)  # multi-user signing key required
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[None, _user_row(id=7, email="bob@x.com", role="user")])
    conn.execute = AsyncMock()
    monkeypatch.setattr(admin_router, "send_magic_link", AsyncMock())

    calls = _patch_audit(monkeypatch)

    pool = _build_mock_pool_txn(conn)
    request = _build_request(pool, user_id=1, user_role="admin")
    body = admin_router.InviteUserBody(email="bob@x.com", role="user")
    await admin_router.invite_user(body, request)

    assert any(c["action"] == "admin.user.invite" for c in calls)
    inv = next(c for c in calls if c["action"] == "admin.user.invite")
    assert inv["resource"] == "users/7"
    assert inv["user_id"] == "1"


@pytest.mark.asyncio
async def test_update_role_writes_audit_with_old_new(monkeypatch) -> None:
    conn = AsyncMock()
    # Promotion (user → admin): old-role lookup returns "user", so the
    # last-admin guard is skipped and admin_count is never read.
    conn.fetchval = AsyncMock(return_value="user")
    conn.fetchrow = AsyncMock(return_value=_user_row(id=2, role="admin"))

    calls = _patch_audit(monkeypatch)

    pool = _build_mock_pool_txn(conn)
    request = _build_request(pool, user_id=1, user_role="admin")
    body = admin_router.UpdateRoleBody(role="admin")
    await admin_router.update_user_role(2, body, request)

    rc = next(c for c in calls if c["action"] == "admin.user.role_change")
    assert rc["metadata"] == {"old_role": "user", "new_role": "admin"}
    assert rc["resource"] == "users/2"


@pytest.mark.asyncio
async def test_soft_delete_writes_audit(monkeypatch) -> None:
    from fastapi import Response

    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value="user")  # non-admin target: last-admin guard skipped
    conn.execute = AsyncMock(return_value="UPDATE 1")

    calls = _patch_audit(monkeypatch)

    pool = _build_mock_pool_txn(conn)
    request = _build_request(pool, user_id=1, user_role="admin")
    await admin_router.soft_delete_user(5, request, Response())

    sd = next(c for c in calls if c["action"] == "admin.user.soft_delete")
    assert sd["resource"] == "users/5"
    assert sd["user_id"] == "1"


@pytest.mark.asyncio
async def test_soft_delete_targets_the_deleted_users_sessions(monkeypatch) -> None:
    """Exactly one session statement is issued, bound to the deleted user.

    The behavioural scoping proof — other users stay signed in — lives in
    tests/contract/test_session_revocation_contract.py against a real database.
    """
    from fastapi import Response

    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value="user")
    conn.execute = AsyncMock(return_value="UPDATE 1")

    _patch_audit(monkeypatch)

    pool = _build_mock_pool_txn(conn)
    request = _build_request(pool, user_id=1, user_role="admin")
    await admin_router.soft_delete_user(5, request, Response())

    session_calls = [call for call in conn.execute.await_args_list if "sessions" in call.args[0]]
    assert len(session_calls) == 1
    statement, *bound = session_calls[0].args
    assert bound == [5]
    # An unscoped revocation signs out every account on the instance. A mock cannot
    # observe which rows a statement would touch, but it can observe that every value
    # bound is one the statement actually consumes: dropping the user predicate leaves
    # the id bound to a statement with no placeholder for it.
    placeholders = {int(match) for match in re.findall(r"\$(\d+)", statement)}
    assert placeholders == set(range(1, len(bound) + 1))


def _patch_audit(monkeypatch) -> list[dict]:
    """Record both best-effort and transaction-bound audit calls."""
    calls: list[dict] = []

    async def _recorder(pool, **kw):
        calls.append(kw)

    monkeypatch.setattr(admin_router, "log_audit", _recorder)
    monkeypatch.setattr(admin_router, "log_audit_strict", _recorder)
    return calls
