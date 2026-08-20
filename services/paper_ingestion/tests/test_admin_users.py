"""Admin user-management router unit tests.

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

import platform_api.routers.admin as admin_router
import pytest
from fastapi import HTTPException, Response
from jarvis_common.email import MagicLinkDelivery
from jarvis_common.owner import OwnerIdentity

from tests._auth_fakes import (
    build_mock_pool,
    build_mock_pool_with_txn,
    build_request_admin,
)

# ---------------------------------------------------------------------------
# Test helpers — pool/request stubs delegated to shared _auth_fakes (D5-03)
# ---------------------------------------------------------------------------

# Fixed stand-in for "now": row timestamps must not depend on when the suite runs.
_FIXED_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

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
        "created_at": _FIXED_NOW - timedelta(days=1),
        "last_login_at": None,
        "deleted_at": deleted_at,
    }


# ---------------------------------------------------------------------------
# require_admin dependency
# ---------------------------------------------------------------------------


# Collapsed: test_require_admin_allows_admin_role
# Survivor: test_admin_contract.py::test_a4_list_users_returns_non_deleted_users
# Allow path covered by admin_client fixture successfully accessing any A4 endpoint.

# Collapsed: test_require_admin_rejects_user_role
# Survivor: test_admin_contract.py::test_a4_list_users_non_admin_gets_403
# Plain-user 403 behavioral outcome covered by contract test_a4 with real DB.


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


# Collapsed: test_list_users_returns_non_deleted_users
# Survivor: test_admin_contract.py::test_a4_list_users_returns_non_deleted_users


# ---------------------------------------------------------------------------
# POST /api/admin/users
# ---------------------------------------------------------------------------


# Collapsed: test_invite_user_happy_path
# Survivor: test_admin_contract.py::test_a5_invite_user_creates_db_row
# SQL-text assertion ("INSERT INTO magic_link_tokens" in sql) — SQL-substring class.
# Contract A5 verifies users row + magic_link_tokens row in real DB; both mock send_magic_link.

# Collapsed: test_invite_user_conflict_raises_409
# Survivor: test_admin_contract.py::test_a5_invite_user_409_on_duplicate_email


# ---------------------------------------------------------------------------
# PATCH /api/admin/users/{id}/role
# ---------------------------------------------------------------------------


# Collapsed: test_update_role_happy_path
# Survivor: test_admin_contract.py::test_a6_update_role_persists_to_db


@pytest.mark.asyncio
async def test_update_role_demote_self_last_admin_blocked() -> None:
    conn = AsyncMock()
    # First fetchval = target's current role; second = admin headcount.
    conn.fetchval = AsyncMock(side_effect=["admin", 1])
    pool = _build_mock_pool_txn(conn)
    request = _build_request(pool, user_id=1, user_role="admin")

    body = admin_router.UpdateRoleBody(role="user")
    with pytest.raises(HTTPException) as exc:
        await admin_router.update_user_role(1, body, request)  # demoting self (id=1)
    assert exc.value.status_code == 400
    assert "last admin" in exc.value.detail


@pytest.mark.asyncio
async def test_update_role_demote_cross_admin_last_admin_blocked() -> None:
    """Guard fires for demoting ANY final admin, not only the caller themselves."""
    conn = AsyncMock()
    conn.fetchval = AsyncMock(side_effect=["admin", 1])  # target is admin; one admin left
    pool = _build_mock_pool_txn(conn)
    request = _build_request(pool, user_id=1, user_role="admin")

    body = admin_router.UpdateRoleBody(role="user")
    with pytest.raises(HTTPException) as exc:
        await admin_router.update_user_role(2, body, request)  # caller 1 demotes admin 2
    assert exc.value.status_code == 400
    assert "last admin" in exc.value.detail


# Collapsed: test_update_role_user_not_found_raises_404
# Survivor: test_admin_contract.py::test_a6_update_role_404_on_nonexistent_user


# ---------------------------------------------------------------------------
# DELETE /api/admin/users/{id}
# ---------------------------------------------------------------------------


# Collapsed: test_soft_delete_self_blocked
# Survivor: test_admin_contract.py::test_a7_self_delete_returns_400


# Collapsed: test_soft_delete_happy_path
# Survivor: test_admin_contract.py::test_a7_soft_delete_sets_deleted_at
# SQL assertion ("deleted_at = NOW()" in sql) — SQL-substring class. Contract A7 verifies deleted_at
# is non-NULL in DB + 204 response after soft-delete.


@pytest.mark.asyncio
async def test_soft_delete_not_found_raises_404() -> None:
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=None)  # target missing or already deleted
    pool = _build_mock_pool_txn(conn)
    request = _build_request(pool, user_id=1, user_role="admin")

    with pytest.raises(HTTPException) as exc:
        await admin_router.soft_delete_user(999, request, Response())
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_soft_delete_last_admin_blocked() -> None:
    """Deleting the final admin (a non-self target) is refused with 400.

    Regression guard: drop the last-admin check in soft_delete_user and this
    deletion passes through (204) instead of 400.
    """
    conn = AsyncMock()
    conn.fetchval = AsyncMock(side_effect=["admin", 1])  # target admin; one admin left
    conn.execute = AsyncMock()
    pool = _build_mock_pool_txn(conn)
    request = _build_request(pool, user_id=1, user_role="admin")

    with pytest.raises(HTTPException) as exc:
        await admin_router.soft_delete_user(2, request, Response())  # caller 1 deletes admin 2
    assert exc.value.status_code == 400
    assert "last admin" in exc.value.detail
    # The guard raises before the soft-delete UPDATE; the row-unchanged behaviour
    # is verified end-to-end in test_admin_contract.py::test_a7_delete_last_admin.


# ---------------------------------------------------------------------------
# Non-admin access
# ---------------------------------------------------------------------------


# Collapsed: test_non_admin_cannot_list_users
# Survivor: test_admin_contract.py::test_a4_list_users_non_admin_gets_403


@pytest.mark.asyncio
async def test_unauthenticated_cannot_reach_admin() -> None:
    """No session = no user_role = 403."""
    conn = AsyncMock()
    pool = _build_mock_pool_txn(conn)
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
        return MagicLinkDelivery.DELIVERED

    monkeypatch.setattr(admin_router, "send_magic_link", fake_send)
    audit = AsyncMock()
    monkeypatch.setattr(admin_router, "log_audit", audit)

    pool = _build_mock_pool(conn)
    request = _build_request(pool, user_id=1, user_role="admin")

    result = await admin_router.send_sign_in_link(5, request)

    assert result.sent is True
    assert result.sent_link is None
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
    monkeypatch.setattr(
        admin_router, "send_magic_link", AsyncMock(return_value=MagicLinkDelivery.DELIVERED)
    )
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
async def test_send_sign_in_link_returns_link_when_smtp_undeliverable(monkeypatch) -> None:
    monkeypatch.setenv("APP_BASE_URL", "https://localhost:3001")
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"id": 5, "email": "bob@example.com"})
    conn.execute = AsyncMock()
    monkeypatch.setattr(
        admin_router,
        "send_magic_link",
        AsyncMock(return_value=MagicLinkDelivery.DROPPED_UNCONFIGURED),
    )
    monkeypatch.setattr(admin_router, "log_audit", AsyncMock())

    pool = _build_mock_pool(conn)
    request = _build_request(pool, user_id=1, user_role="admin")

    result = await admin_router.send_sign_in_link(5, request)

    assert result.sent is False
    assert result.sent_link is not None
    assert "token=" in result.sent_link


@pytest.mark.asyncio
async def test_send_sign_in_link_hides_link_when_deliverable(monkeypatch) -> None:
    monkeypatch.setenv("APP_BASE_URL", "https://localhost:3001")
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"id": 5, "email": "bob@example.com"})
    conn.execute = AsyncMock()
    monkeypatch.setattr(
        admin_router, "send_magic_link", AsyncMock(return_value=MagicLinkDelivery.DELIVERED)
    )
    monkeypatch.setattr(admin_router, "log_audit", AsyncMock())

    pool = _build_mock_pool(conn)
    request = _build_request(pool, user_id=1, user_role="admin")

    result = await admin_router.send_sign_in_link(5, request)

    assert result.sent is True
    assert result.sent_link is None


def _send_link_stubs(monkeypatch, owner: OwnerIdentity):
    """Wire send_sign_in_link for an owner-targeting case.

    Returns ``(conn, audit)`` so a case can assert on the token INSERT and the
    audit row as well as on the response.
    """
    monkeypatch.setenv("APP_BASE_URL", "https://localhost:3001")
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"id": 5, "email": "bob@example.com"})
    conn.execute = AsyncMock()
    monkeypatch.setattr(admin_router, "resolve_owner_identity", AsyncMock(return_value=owner))
    monkeypatch.setattr(
        admin_router, "send_magic_link", AsyncMock(return_value=MagicLinkDelivery.DELIVERED)
    )
    audit = AsyncMock()
    monkeypatch.setattr(admin_router, "log_audit", audit)
    return conn, audit


@pytest.mark.asyncio
async def test_send_link_for_the_owner_refused_for_another_admin(monkeypatch) -> None:
    """A non-owner admin cannot obtain the owner's sign-in link.

    The refusal must land before the token is minted: a 409 that still wrote a
    magic_link_tokens row would let any admin mint owner login tokens at will,
    and one that still wrote an audit row would record links nobody sent.
    """
    conn, audit = _send_link_stubs(
        monkeypatch, OwnerIdentity(source="database", state="valid", user_id=5)
    )
    request = _build_request(_build_mock_pool(conn), user_id=1, user_role="admin")

    with pytest.raises(HTTPException) as exc:
        await admin_router.send_sign_in_link(5, request)

    assert exc.value.status_code == 409
    assert "owner" in exc.value.detail.lower()
    conn.execute.assert_not_called()
    audit.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_link_for_the_owner_allowed_for_the_owner(monkeypatch) -> None:
    conn, _audit = _send_link_stubs(
        monkeypatch, OwnerIdentity(source="database", state="valid", user_id=5)
    )
    request = _build_request(_build_mock_pool(conn), user_id=5, user_role="admin")

    result = await admin_router.send_sign_in_link(5, request)

    assert result.sent is True
    conn.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_link_for_a_non_owner_target_is_unaffected(monkeypatch) -> None:
    conn, _audit = _send_link_stubs(
        monkeypatch, OwnerIdentity(source="database", state="valid", user_id=7)
    )
    request = _build_request(_build_mock_pool(conn), user_id=1, user_role="admin")

    result = await admin_router.send_sign_in_link(5, request)

    assert result.sent is True
    conn.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_link_not_refused_when_the_owner_config_is_stale(monkeypatch) -> None:
    """A configured-but-unresolvable owner must fail OPEN.

    ``user_id`` is populated even for an invalid owner target, so gating on it
    directly would 409 the very recovery an admin needs when the owner setting
    points at a non-admin or deleted user.
    """
    conn, _audit = _send_link_stubs(
        monkeypatch, OwnerIdentity(source="database", state="non_admin_user", user_id=5)
    )
    request = _build_request(_build_mock_pool(conn), user_id=1, user_role="admin")

    result = await admin_router.send_sign_in_link(5, request)

    assert result.sent is True
    conn.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_non_admin_cannot_send_link() -> None:
    conn = AsyncMock()
    pool = _build_mock_pool(conn)
    request = _build_request(pool, user_id=2, user_role="user")
    with pytest.raises(HTTPException) as exc:
        await admin_router.require_admin(request)
    assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# B2/OPS-2: invite deliverability — surface the link only when undeliverable
# ---------------------------------------------------------------------------


def _invite_conn() -> AsyncMock:
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            None,  # conflict check: no existing user
            {
                "id": 7,
                "email": "bob@example.com",
                "role": "user",
                "created_at": _FIXED_NOW,
                "last_login_at": None,
            },
        ]
    )
    conn.execute = AsyncMock()
    return conn


@pytest.mark.asyncio
async def test_invite_returns_link_when_smtp_unconfigured(monkeypatch) -> None:
    """SMTP unconfigured → the admin gets the link back to share manually."""
    monkeypatch.setenv("APP_BASE_URL", "https://localhost:3001")
    monkeypatch.setenv("JARVIS_MODEL_HMAC_KEY", "x" * 32)
    monkeypatch.setattr(
        admin_router,
        "send_magic_link",
        AsyncMock(return_value=MagicLinkDelivery.DROPPED_UNCONFIGURED),
    )
    monkeypatch.setattr(admin_router, "log_audit", AsyncMock())

    pool = _build_mock_pool_txn(_invite_conn())
    request = _build_request(pool, user_id=1, user_role="admin")
    body = admin_router.InviteUserBody(email="bob@example.com", role="user")

    result = await admin_router.invite_user(body, request)

    assert result.email == "bob@example.com"
    assert result.invite_link is not None
    assert "token=" in result.invite_link


@pytest.mark.asyncio
async def test_invite_omits_link_when_smtp_configured(monkeypatch) -> None:
    """SMTP configured + send succeeds → no link leaked in the response."""
    monkeypatch.setenv("JARVIS_MODEL_HMAC_KEY", "x" * 32)
    monkeypatch.setattr(
        admin_router, "send_magic_link", AsyncMock(return_value=MagicLinkDelivery.DELIVERED)
    )
    monkeypatch.setattr(admin_router, "log_audit", AsyncMock())

    pool = _build_mock_pool_txn(_invite_conn())
    request = _build_request(pool, user_id=1, user_role="admin")
    body = admin_router.InviteUserBody(email="bob@example.com", role="user")

    result = await admin_router.invite_user(body, request)

    assert result.invite_link is None


@pytest.mark.asyncio
async def test_invite_returns_link_when_smtp_send_fails(monkeypatch) -> None:
    """SMTP nominally configured but the send raises → still surface the link."""
    monkeypatch.setenv("APP_BASE_URL", "https://localhost:3001")
    monkeypatch.setenv("JARVIS_MODEL_HMAC_KEY", "x" * 32)

    async def _failing_send(email, link, *, pool=None):
        raise OSError("SMTP connect refused")

    monkeypatch.setattr(admin_router, "send_magic_link", _failing_send)
    monkeypatch.setattr(admin_router, "log_audit", AsyncMock())

    pool = _build_mock_pool_txn(_invite_conn())
    request = _build_request(pool, user_id=1, user_role="admin")
    body = admin_router.InviteUserBody(email="bob@example.com", role="user")

    result = await admin_router.invite_user(body, request)

    assert result.invite_link is not None
    assert "token=" in result.invite_link


@pytest.mark.asyncio
async def test_invite_returns_link_when_dev_log_only_drops_delivery(monkeypatch) -> None:
    """DEV_SMTP_LOG_ONLY drops delivery even with a configured relay → surface the link.

    Config presence is not delivery: the sender returns DROPPED_DEV_LOG_ONLY, so
    the admin must still get the manual link back.
    """
    monkeypatch.setenv("APP_BASE_URL", "https://localhost:3001")
    monkeypatch.setenv("JARVIS_MODEL_HMAC_KEY", "x" * 32)
    monkeypatch.setattr(
        admin_router,
        "send_magic_link",
        AsyncMock(return_value=MagicLinkDelivery.DROPPED_DEV_LOG_ONLY),
    )
    monkeypatch.setattr(admin_router, "log_audit", AsyncMock())

    pool = _build_mock_pool_txn(_invite_conn())
    request = _build_request(pool, user_id=1, user_role="admin")
    body = admin_router.InviteUserBody(email="bob@example.com", role="user")

    result = await admin_router.invite_user(body, request)

    assert result.invite_link is not None
    assert "token=" in result.invite_link


@pytest.mark.asyncio
async def test_build_invite_link_warns_on_unset_app_base_url_in_production(
    monkeypatch, caplog
) -> None:
    """Production without APP_BASE_URL → the admin logger warns about the invite link.

    The warning must stay on this module's logger and name the invite link, so
    operators filtering by logger name still see it and can tell it apart from
    the sign-in-link warning the auth router emits.
    """
    import logging
    from types import SimpleNamespace

    monkeypatch.delenv("APP_BASE_URL", raising=False)
    monkeypatch.setenv("ENVIRONMENT", "production")

    request = SimpleNamespace(
        url=SimpleNamespace(
            replace=lambda **kw: (
                "https://origin/auth/verify" + (f"?{kw['query']}" if kw.get("query") else "")
            )
        )
    )

    with caplog.at_level(logging.WARNING, logger="platform_api.routers.admin"):
        link = admin_router._build_invite_link(request, "tok123")

    assert link == "https://origin/auth/verify#token=tok123"
    assert "?token=" not in link
    warnings = [
        r
        for r in caplog.records
        if r.name == "platform_api.routers.admin" and r.levelno == logging.WARNING
    ]
    assert len(warnings) == 1, (
        "Expected exactly one warning on the admin logger; got "
        f"{[(r.name, r.message) for r in caplog.records]}"
    )
    assert "APP_BASE_URL" in warnings[0].message
    assert "invite link" in warnings[0].message, (
        f"the warning must name the invite link; got: {warnings[0].message!r}"
    )


# ---------------------------------------------------------------------------
# Invite SMTP failure log must not contain raw email
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invite_smtp_failure_logs_hash_not_raw_email(monkeypatch, caplog) -> None:
    """On SMTP failure, logger.exception must record email_hash, never the raw address."""
    import hashlib
    import logging

    monkeypatch.setenv("DEV_MODE", "true")
    monkeypatch.setenv("JARVIS_MODEL_HMAC_KEY", "x" * 32)
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            None,  # conflict check: no existing user
            {
                "id": 7,
                "email": "bob@example.com",
                "role": "user",
                "created_at": _FIXED_NOW,
                "last_login_at": None,
            },
        ]
    )
    conn.execute = AsyncMock()

    async def _failing_send(email, link, *, pool=None):
        raise OSError("SMTP connect refused")

    monkeypatch.setattr(admin_router, "send_magic_link", _failing_send)
    monkeypatch.setattr(admin_router, "log_audit", AsyncMock())

    pool = _build_mock_pool_txn(conn)
    request = _build_request(pool, user_id=1, user_role="admin")
    body = admin_router.InviteUserBody(email="bob@example.com", role="user")

    expected_hash = hashlib.sha256(b"bob@example.com").hexdigest()

    with caplog.at_level(logging.ERROR, logger="platform_api.routers.admin"):
        await admin_router.invite_user(body, request)

    assert any(expected_hash in r.message for r in caplog.records), (
        "Expected email hash in log record"
    )
    assert not any("bob@example.com" in r.message for r in caplog.records), (
        "Raw email must not appear in any log record"
    )


# ---------------------------------------------------------------------------
# Invite + restore require a real Pulse model-signing key (multi-user gate)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invite_user_blocked_without_model_hmac_key(monkeypatch) -> None:
    """No JARVIS_MODEL_HMAC_KEY → invite rejected with 409 before any user row is created."""
    monkeypatch.delenv("JARVIS_MODEL_HMAC_KEY", raising=False)
    monkeypatch.delenv("JARVIS_MODEL_HMAC_KEY_FILE", raising=False)
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)  # conflict check: no existing user
    conn.execute = AsyncMock()
    monkeypatch.setattr(admin_router, "send_magic_link", AsyncMock())
    monkeypatch.setattr(admin_router, "log_audit", AsyncMock())

    pool = _build_mock_pool_txn(conn)
    request = _build_request(pool, user_id=1, user_role="admin")
    body = admin_router.InviteUserBody(email="bob@example.com", role="user")

    with pytest.raises(HTTPException) as exc:
        await admin_router.invite_user(body, request)
    assert exc.value.status_code == 409
    assert "JARVIS_MODEL_HMAC_KEY" in exc.value.detail
    # Gate fires before the INSERT — no magic-link token row written.
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_invite_user_allowed_with_model_hmac_key(monkeypatch) -> None:
    """With a real JARVIS_MODEL_HMAC_KEY the invite proceeds to create the user."""
    monkeypatch.setenv("APP_BASE_URL", "https://localhost:3001")
    monkeypatch.setenv("JARVIS_MODEL_HMAC_KEY", "x" * 32)
    monkeypatch.setattr(admin_router, "send_magic_link", AsyncMock())
    monkeypatch.setattr(admin_router, "log_audit", AsyncMock())

    pool = _build_mock_pool_txn(_invite_conn())
    request = _build_request(pool, user_id=1, user_role="admin")
    body = admin_router.InviteUserBody(email="bob@example.com", role="user")

    result = await admin_router.invite_user(body, request)
    assert result.email == "bob@example.com"


@pytest.mark.asyncio
async def test_restore_user_blocked_without_model_hmac_key(monkeypatch) -> None:
    """No JARVIS_MODEL_HMAC_KEY → restore rejected with 409 before the UPDATE."""
    monkeypatch.delenv("JARVIS_MODEL_HMAC_KEY", raising=False)
    monkeypatch.delenv("JARVIS_MODEL_HMAC_KEY_FILE", raising=False)
    conn = AsyncMock()
    conn.fetchrow = AsyncMock()
    monkeypatch.setattr(admin_router, "log_audit", AsyncMock())

    pool = _build_mock_pool(conn)
    request = _build_request(pool, user_id=1, user_role="admin")

    with pytest.raises(HTTPException) as exc:
        await admin_router.restore_user(7, request)
    assert exc.value.status_code == 409
    assert "JARVIS_MODEL_HMAC_KEY" in exc.value.detail
    # Gate fires before the UPDATE — deleted_at is never cleared.
    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_restore_user_allowed_with_model_hmac_key(monkeypatch) -> None:
    """With a real JARVIS_MODEL_HMAC_KEY the restore proceeds and clears deleted_at."""
    monkeypatch.setenv("JARVIS_MODEL_HMAC_KEY", "x" * 32)
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=None)
    conn.fetchrow = AsyncMock(
        side_effect=[
            {
                "id": 7,
                "email": "bob@example.com",
                "role": "user",
                "created_at": _FIXED_NOW,
                "last_login_at": None,
                "deleted_at": _FIXED_NOW,
            },
            {
                "id": 7,
                "email": "bob@example.com",
                "role": "user",
                "created_at": _FIXED_NOW,
                "last_login_at": None,
            },
        ]
    )
    monkeypatch.setattr(admin_router, "log_audit", AsyncMock())

    pool = _build_mock_pool_txn(conn)
    request = _build_request(pool, user_id=1, user_role="admin")

    result = await admin_router.restore_user(7, request)
    assert result.id == 7
    assert result.email == "bob@example.com"
    assert conn.fetchrow.await_count == 2
