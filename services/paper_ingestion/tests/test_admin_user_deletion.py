"""User-deletion: restore endpoint + admin audit-log coverage.

Reuses the mocked-pool style from test_admin_users.py (no Docker needed).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import paper_ingestion.routers.admin as admin_router
import pytest
from fastapi import HTTPException

from tests._auth_fakes import build_mock_pool, build_request_admin

_NOW = datetime.now(UTC)

# Pool/request stubs delegated to shared _auth_fakes (D5-03).
_build_mock_pool = build_mock_pool
_build_request = build_request_admin


def _user_row(*, id=2, email="a@x.com", role="user") -> dict:
    return {
        "id": id,
        "email": email,
        "role": role,
        "created_at": _NOW - timedelta(days=1),
        "last_login_at": None,
    }


# --------------------------------------------------------------------------
# restore endpoint
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restore_clears_deleted_at_within_grace() -> None:
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=_user_row(id=5))
    pool = _build_mock_pool(conn)
    request = _build_request(pool, user_id=1, user_role="admin")

    result = await admin_router.restore_user(5, request)

    assert result.id == 5
    sql = conn.fetchrow.await_args.args[0]
    assert "deleted_at = NULL" in sql
    assert "deleted_at IS NOT NULL" in sql
    assert "30 days" in sql


@pytest.mark.asyncio
async def test_restore_not_found_or_past_grace_raises_404() -> None:
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    pool = _build_mock_pool(conn)
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
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[None, _user_row(id=7, email="bob@x.com", role="user")])
    conn.execute = AsyncMock()
    monkeypatch.setattr(admin_router, "send_magic_link", AsyncMock())

    calls = _patch_audit(monkeypatch)

    pool = _build_mock_pool(conn)
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
    # caller (1) != target (2) → self-demotion guard skipped, so the only
    # fetchval is the old-role lookup.
    conn.fetchval = AsyncMock(return_value="user")
    conn.fetchrow = AsyncMock(return_value=_user_row(id=2, role="admin"))

    calls = _patch_audit(monkeypatch)

    pool = _build_mock_pool(conn)
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
    conn.execute = AsyncMock(return_value="UPDATE 1")

    calls = _patch_audit(monkeypatch)

    pool = _build_mock_pool(conn)
    request = _build_request(pool, user_id=1, user_role="admin")
    await admin_router.soft_delete_user(5, request, Response())

    sd = next(c for c in calls if c["action"] == "admin.user.soft_delete")
    assert sd["resource"] == "users/5"
    assert sd["user_id"] == "1"


def _patch_audit(monkeypatch) -> list[dict]:
    """Replace admin_router.log_audit with an async recorder; return the list."""
    calls: list[dict] = []

    async def _recorder(pool, **kw):
        calls.append(kw)

    monkeypatch.setattr(admin_router, "log_audit", _recorder)
    return calls
