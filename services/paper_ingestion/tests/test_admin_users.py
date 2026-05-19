"""Phase 2 WS-2B: admin user-management router unit tests.

Mocks the DB pool so the suite runs without Docker.

Coverage:
- GET /api/admin/users  — list non-deleted users
- POST /api/admin/users — invite user (happy path + duplicate conflict)
- PATCH /api/admin/users/{id}/role — change role; demote-self guard
- DELETE /api/admin/users/{id}  — soft delete; self-delete guard
- 403 when non-admin or unauthenticated hits any endpoint
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import paper_ingestion.routers.admin as admin_router
import pytest
from fastapi import HTTPException, Response

from tests._auth_fakes import build_mock_pool, build_request_admin

# ---------------------------------------------------------------------------
# Test helpers — pool/request stubs delegated to shared _auth_fakes (D5-03)
# ---------------------------------------------------------------------------

_NOW = datetime.now(UTC)

_build_mock_pool = build_mock_pool
_build_request = build_request_admin


def _user_row(
    *,
    id: int = 1,
    email: str = "alice@example.com",
    role: str = "user",
    deleted_at: datetime | None = None,
) -> dict:
    return {
        "id": id,
        "email": email,
        "role": role,
        "created_at": _NOW - timedelta(days=1),
        "last_login_at": None,
        "deleted_at": deleted_at,
    }


# ---------------------------------------------------------------------------
# require_admin dependency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_require_admin_allows_admin_role() -> None:
    conn = AsyncMock()
    request = _build_request(_build_mock_pool(conn), user_role="admin")
    # Must not raise
    await admin_router.require_admin(request)


@pytest.mark.asyncio
async def test_require_admin_rejects_user_role() -> None:
    conn = AsyncMock()
    request = _build_request(_build_mock_pool(conn), user_role="user")
    with pytest.raises(HTTPException) as exc:
        await admin_router.require_admin(request)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_require_admin_rejects_missing_state() -> None:
    conn = AsyncMock()
    request = _build_request(_build_mock_pool(conn))  # no role set
    with pytest.raises(HTTPException) as exc:
        await admin_router.require_admin(request)
    assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# GET /api/admin/users
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_users_returns_non_deleted_users() -> None:
    conn = AsyncMock()
    conn.fetch = AsyncMock(
        return_value=[
            _user_row(id=1, email="admin@example.com", role="admin"),
            _user_row(id=2, email="alice@example.com", role="user"),
        ]
    )
    pool = _build_mock_pool(conn)
    request = _build_request(pool, user_id=1, user_role="admin")

    result = await admin_router.list_users(request)

    assert len(result) == 2
    assert result[0].email == "admin@example.com"
    assert result[1].email == "alice@example.com"
    sql = conn.fetch.await_args.args[0]
    assert "deleted_at IS NULL" in sql


# ---------------------------------------------------------------------------
# POST /api/admin/users
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invite_user_happy_path(monkeypatch) -> None:
    monkeypatch.setenv("DEV_MODE", "true")
    monkeypatch.setenv("APP_BASE_URL", "https://localhost:3001")

    conn = AsyncMock()
    # No conflict
    conn.fetchrow = AsyncMock(
        side_effect=[
            None,  # conflict check → no existing user
            _user_row(id=5, email="bob@example.com", role="user"),  # insert RETURNING
        ]
    )
    conn.execute = AsyncMock()

    sent: list[tuple[str, str]] = []

    async def fake_send(email, link, *, pool=None):
        sent.append((email, link))

    monkeypatch.setattr(admin_router, "send_magic_link", fake_send)
    # WS-USER-DELETION added a best-effort audit write; stub it so the
    # conn.execute count below stays scoped to the invite's own DB activity.
    monkeypatch.setattr(admin_router, "log_audit", AsyncMock())

    pool = _build_mock_pool(conn)
    request = _build_request(pool, user_id=1, user_role="admin")

    body = admin_router.InviteUserBody(email="bob@example.com", role="user")
    result = await admin_router.invite_user(body, request)

    assert result.id == 5
    assert result.email == "bob@example.com"
    assert result.role == "user"
    # Token row inserted
    conn.execute.assert_awaited_once()
    insert_sql = conn.execute.await_args.args[0]
    assert "INSERT INTO magic_link_tokens" in insert_sql
    # Invite sent
    assert len(sent) == 1
    assert sent[0][0] == "bob@example.com"
    assert "token=" in sent[0][1]


@pytest.mark.asyncio
async def test_invite_user_conflict_raises_409(monkeypatch) -> None:
    monkeypatch.setenv("DEV_MODE", "true")

    conn = AsyncMock()
    # Conflict check returns existing user
    conn.fetchrow = AsyncMock(return_value={"id": 3})

    pool = _build_mock_pool(conn)
    request = _build_request(pool, user_id=1, user_role="admin")

    body = admin_router.InviteUserBody(email="existing@example.com", role="user")
    with pytest.raises(HTTPException) as exc:
        await admin_router.invite_user(body, request)
    assert exc.value.status_code == 409


# ---------------------------------------------------------------------------
# PATCH /api/admin/users/{id}/role
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_role_happy_path() -> None:
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=2)  # 2 admins → safe to demote
    conn.fetchrow = AsyncMock(return_value=_user_row(id=2, email="alice@example.com", role="user"))
    pool = _build_mock_pool(conn)
    request = _build_request(pool, user_id=1, user_role="admin")

    body = admin_router.UpdateRoleBody(role="user")
    result = await admin_router.update_user_role(2, body, request)

    assert result.role == "user"
    assert result.id == 2


@pytest.mark.asyncio
async def test_update_role_demote_self_last_admin_blocked() -> None:
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=1)  # only 1 admin
    pool = _build_mock_pool(conn)
    request = _build_request(pool, user_id=1, user_role="admin")

    body = admin_router.UpdateRoleBody(role="user")
    with pytest.raises(HTTPException) as exc:
        await admin_router.update_user_role(1, body, request)  # demoting self (id=1)
    assert exc.value.status_code == 400
    assert "last admin" in exc.value.detail


@pytest.mark.asyncio
async def test_update_role_user_not_found_raises_404() -> None:
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=2)  # 2 admins (for safe demote path)
    conn.fetchrow = AsyncMock(return_value=None)  # UPDATE RETURNING → None
    pool = _build_mock_pool(conn)
    request = _build_request(pool, user_id=1, user_role="admin")

    body = admin_router.UpdateRoleBody(role="admin")
    with pytest.raises(HTTPException) as exc:
        await admin_router.update_user_role(999, body, request)
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /api/admin/users/{id}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_soft_delete_self_blocked() -> None:
    conn = AsyncMock()
    pool = _build_mock_pool(conn)
    request = _build_request(pool, user_id=7, user_role="admin")

    with pytest.raises(HTTPException) as exc:
        await admin_router.soft_delete_user(7, request, Response())
    assert exc.value.status_code == 400
    assert "own account" in exc.value.detail


@pytest.mark.asyncio
async def test_soft_delete_happy_path(monkeypatch) -> None:
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value="UPDATE 1")
    pool = _build_mock_pool(conn)
    request = _build_request(pool, user_id=1, user_role="admin")
    # WS-USER-DELETION added a best-effort audit write; stub it so the
    # asserted execute below is the soft-delete UPDATE, not the audit INSERT.
    monkeypatch.setattr(admin_router, "log_audit", AsyncMock())

    # Should not raise; DELETE returns 204 Response
    result = await admin_router.soft_delete_user(5, request, Response())
    assert result is not None  # returns a Response object
    assert result.status_code == 204

    update_sql = conn.execute.await_args.args[0]
    assert "deleted_at = NOW()" in update_sql


@pytest.mark.asyncio
async def test_soft_delete_not_found_raises_404() -> None:
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value="UPDATE 0")
    pool = _build_mock_pool(conn)
    request = _build_request(pool, user_id=1, user_role="admin")

    with pytest.raises(HTTPException) as exc:
        await admin_router.soft_delete_user(999, request, Response())
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# Non-admin access
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_admin_cannot_list_users() -> None:
    conn = AsyncMock()
    pool = _build_mock_pool(conn)
    request = _build_request(pool, user_id=2, user_role="user")
    with pytest.raises(HTTPException) as exc:
        await admin_router.require_admin(request)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_unauthenticated_cannot_reach_admin() -> None:
    """No session = no user_role = 403."""
    conn = AsyncMock()
    pool = _build_mock_pool(conn)
    request = _build_request(pool)  # no role, no user_id
    with pytest.raises(HTTPException) as exc:
        await admin_router.require_admin(request)
    assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# POST /api/admin/users/{id}/send-link
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_link_issues_short_token_and_emails(monkeypatch) -> None:
    monkeypatch.setenv("DEV_MODE", "true")
    monkeypatch.setenv("APP_BASE_URL", "https://localhost:3001")

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"id": 5, "email": "bob@example.com"})
    conn.execute = AsyncMock()

    sent: list[tuple[str, str]] = []

    async def fake_send(email, link, *, pool=None):
        sent.append((email, link))

    monkeypatch.setattr(admin_router, "send_magic_link", fake_send)
    audit = AsyncMock()
    monkeypatch.setattr(admin_router, "log_audit", audit)

    pool = _build_mock_pool(conn)
    request = _build_request(pool, user_id=1, user_role="admin")

    result = await admin_router.send_sign_in_link(5, request)

    assert result == {"sent": True}
    # 15-minute magic_link_tokens row inserted (login TTL, not 24h invite).
    conn.execute.assert_awaited_once()
    insert_sql, _hash, uid, expires_at = conn.execute.await_args.args
    assert "INSERT INTO magic_link_tokens" in insert_sql
    assert uid == 5
    ttl = expires_at - datetime.now(UTC)
    assert ttl <= admin_router.MAGIC_LINK_TTL
    assert ttl > timedelta(minutes=10)  # ~15 min, well under the 24h invite TTL
    # Link emailed to the existing user's address.
    assert sent == [("bob@example.com", sent[0][1])]
    assert "token=" in sent[0][1]
    # Audit row written.
    audit.assert_awaited_once()
    assert audit.await_args.kwargs["action"] == "admin.user.send_link"
    assert audit.await_args.kwargs["resource"] == "users/5"


@pytest.mark.asyncio
async def test_send_link_token_has_pending_email_null(monkeypatch) -> None:
    """Login token: INSERT omits pending_email so /auth/verify accepts it."""
    monkeypatch.setenv("DEV_MODE", "true")
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"id": 9, "email": "x@example.com"})
    conn.execute = AsyncMock()
    monkeypatch.setattr(admin_router, "send_magic_link", AsyncMock())
    monkeypatch.setattr(admin_router, "log_audit", AsyncMock())

    pool = _build_mock_pool(conn)
    request = _build_request(pool, user_id=1, user_role="admin")

    await admin_router.send_sign_in_link(9, request)

    insert_sql = conn.execute.await_args.args[0]
    assert "pending_email" not in insert_sql
    # Only (token_hash, user_id, expires_at) bound — pending_email defaults NULL.
    assert len(conn.execute.await_args.args) == 4


@pytest.mark.asyncio
async def test_send_link_missing_or_deleted_user_raises_404(monkeypatch) -> None:
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)  # absent OR deleted_at IS NOT NULL
    monkeypatch.setattr(admin_router, "send_magic_link", AsyncMock())
    monkeypatch.setattr(admin_router, "log_audit", AsyncMock())
    pool = _build_mock_pool(conn)
    request = _build_request(pool, user_id=1, user_role="admin")

    with pytest.raises(HTTPException) as exc:
        await admin_router.send_sign_in_link(999, request)
    assert exc.value.status_code == 404
    conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_non_admin_cannot_send_link() -> None:
    conn = AsyncMock()
    pool = _build_mock_pool(conn)
    request = _build_request(pool, user_id=2, user_role="user")
    with pytest.raises(HTTPException) as exc:
        await admin_router.require_admin(request)
    assert exc.value.status_code == 403
