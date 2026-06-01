"""Admin user-management contract tests — target rows A4-A8, A14.

Covers:
  A4:  GET  /api/admin/users              — list non-deleted users (admin-gated)
  A5:  POST /api/admin/users              — invite user; 409 on duplicate email
  A6:  PATCH /api/admin/users/{id}/role   — role update; 404 on missing user
  A7:  DELETE /api/admin/users/{id}       — soft-delete; 400 on self-delete
  A8:  POST /api/admin/users/{id}/restore — restore within 30-day grace; 404 past it
  A14: GET  /api/admin/audit-log          — audit log rows (admin-gated; 403 for non-admin)

Auth wiring:
  admin.py router is included with ``dependencies=[]`` (no global verify_api_key).
  Its ``require_admin`` is a *local* function used via ``Depends(require_admin)``
  in ``dependencies=[Depends(require_admin)]`` at each route.
  To inject admin identity we create a real users+sessions row so the
  SessionMiddleware populates request.state.user_role='admin' from the cookie.

  audit_admin.py router uses ``from jarvis_common.auth import require_admin`` via
  ``Depends(require_admin)`` in its router-level dependencies; we override that
  dependency in the fixture.

Carve-out: send_magic_link (SMTP) is patched out — outbound email boundary exempt.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch
from jarvis_common.testing import SharedConnPool

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


async def _seed_admin_user(conn) -> tuple[int, str]:
    """Insert one admin user + valid session. Returns (user_id, session_cookie)."""
    user_id = await conn.fetchval(
        "INSERT INTO users (email, role) VALUES ($1, 'admin') RETURNING id",
        "admin-contract-test@example.com",
    )
    session_id = await conn.fetchval(
        """INSERT INTO sessions (user_id, expires_at)
           VALUES ($1, NOW() + INTERVAL '1 day') RETURNING id""",
        user_id,
    )
    return int(user_id), str(session_id)


async def _seed_plain_user(conn, email: str) -> tuple[int, str]:
    """Insert one non-admin user + valid session. Returns (user_id, session_cookie)."""
    user_id = await conn.fetchval(
        "INSERT INTO users (email, role) VALUES ($1, 'user') RETURNING id",
        email,
    )
    session_id = await conn.fetchval(
        """INSERT INTO sessions (user_id, expires_at)
           VALUES ($1, NOW() + INTERVAL '1 day') RETURNING id""",
        user_id,
    )
    return int(user_id), str(session_id)


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def admin_client(contract_conn):
    """ASGI client authenticated as an admin via a real session cookie.

    Sets BOTH pool overrides (state + dependency_overrides) so all routes
    reach the shared transactional connection.  Disables the rate limiter.
    Patches send_magic_link to avoid SMTP boundary.
    """
    from jarvis_common import verify_api_key
    from jarvis_common.testing_contract_apps import (
        make_contract_client,
        patch_app_state,
        patch_dependency_overrides,
    )
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app

    admin_user_id, admin_cookie = await _seed_admin_user(contract_conn)
    shared = SharedConnPool(contract_conn)
    app.state.limiter.enabled = False
    try:
        with (
            patch_app_state(app, {"db_pool": shared}),
            patch_dependency_overrides(
                app,
                set_overrides={get_db_pool: lambda: shared, verify_api_key: lambda: None},
            ),
            patch(
                "paper_ingestion.routers.admin.send_magic_link",
                new=AsyncMock(return_value=None),
            ),
        ):
            async with make_contract_client(app, admin_cookie) as client:
                client.admin_user_id = admin_user_id  # type: ignore[attr-defined]
                yield client
    finally:
        app.state.limiter.enabled = True


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def plain_client(contract_conn):
    """ASGI client authenticated as a non-admin user (role='user').

    Used to assert 403 on admin-gated endpoints.
    We also need the admin user in DB so require_unconfigured_or_admin in setup
    doesn't short-circuit on 'no admins' — but for admin.py endpoints that
    guard is not present; the 403 comes purely from role != 'admin'.
    """
    from jarvis_common import verify_api_key
    from jarvis_common.testing_contract_apps import (
        make_contract_client,
        patch_app_state,
        patch_dependency_overrides,
    )
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app

    # Seed admin so the users table is not empty (session middleware needs it).
    await _seed_admin_user(contract_conn)
    _user_id, user_cookie = await _seed_plain_user(contract_conn, "plain-user-contract@example.com")
    shared = SharedConnPool(contract_conn)
    app.state.limiter.enabled = False
    try:
        with (
            patch_app_state(app, {"db_pool": shared}),
            patch_dependency_overrides(
                app,
                set_overrides={get_db_pool: lambda: shared, verify_api_key: lambda: None},
            ),
        ):
            async with make_contract_client(app, user_cookie) as client:
                yield client
    finally:
        app.state.limiter.enabled = True


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def audit_admin_client(contract_conn):
    """ASGI client for audit_admin.py endpoints.

    audit_admin.py uses ``from jarvis_common.auth import require_admin`` in its
    router-level ``dependencies=[Depends(verify_api_key), Depends(require_admin)]``.
    We override require_admin via dependency_overrides and pass X-API-Key.
    """
    from jarvis_common import verify_api_key
    from jarvis_common.auth import require_admin
    from jarvis_common.testing_contract_apps import (
        make_contract_client,
        patch_app_state,
        patch_dependency_overrides,
    )
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app

    async def _allow_all() -> None:
        return None

    shared = SharedConnPool(contract_conn)
    app.state.limiter.enabled = False
    try:
        with (
            patch_app_state(app, {"db_pool": shared}),
            patch_dependency_overrides(
                app,
                set_overrides={
                    get_db_pool: lambda: shared,
                    verify_api_key: lambda: None,
                    require_admin: _allow_all,
                },
            ),
        ):
            async with make_contract_client(app, None) as client:
                yield client
    finally:
        app.state.limiter.enabled = True


# ---------------------------------------------------------------------------
# A4: GET /api/admin/users — admin lists non-deleted users
# ---------------------------------------------------------------------------


async def test_a4_list_users_returns_non_deleted_users(admin_client, contract_conn):
    """Covers map row A4: admin lists all non-deleted users; response shape matches UserRecord.

    Verified: admin.py:133-145 list_users at HEAD.
    Survivor-of: test_admin_users.py list-users mock assertions.
    """
    resp = await admin_client.get("/api/admin/users")

    assert resp.status_code == 200, (
        f"Expected 200 from list_users; got {resp.status_code}: {resp.text[:300]}"
    )
    body = resp.json()
    assert isinstance(body, list)
    # The seeded admin user must appear.
    emails = [u["email"] for u in body]
    assert "admin-contract-test@example.com" in emails, f"Seeded admin not in list: {emails}"
    # All returned users must have required UserRecord fields.
    for user in body:
        for field in ("id", "email", "role", "created_at"):
            assert field in user, f"Missing field {field!r} in user record: {user}"


async def test_a4_list_users_non_admin_gets_403(plain_client):
    """Covers map row A4: non-admin caller gets 403 from admin role gate.

    Verified: admin.py:78-91 require_admin at HEAD.
    """
    resp = await plain_client.get("/api/admin/users")
    assert resp.status_code == 403, (
        f"Expected 403 for non-admin on /api/admin/users; got {resp.status_code}"
    )


# ---------------------------------------------------------------------------
# A5: POST /api/admin/users — admin invites a new user
# ---------------------------------------------------------------------------


async def test_a5_invite_user_creates_db_row(admin_client, contract_conn):
    """Covers map row A5: admin invite creates users row + magic_link_tokens row.

    Verified: admin.py:154-216 invite_user at HEAD.
    Survivor-of: test_admin_users.py invite mock assertions.
    """
    new_email = "invited-contract-user@example.com"

    resp = await admin_client.post(
        "/api/admin/users",
        json={"email": new_email, "role": "user"},
    )

    assert resp.status_code == 201, (
        f"Expected 201 from invite_user; got {resp.status_code}: {resp.text[:300]}"
    )
    body = resp.json()
    assert body["email"] == new_email
    assert body["role"] == "user"
    assert "id" in body

    # Verify the user row landed in DB.
    row = await contract_conn.fetchrow(
        "SELECT id, email, role FROM users WHERE email = $1 AND deleted_at IS NULL",
        new_email,
    )
    assert row is not None, "Invited user must exist in DB"
    assert row["role"] == "user"

    # Verify a magic_link_tokens row was created for the invite.
    token_row = await contract_conn.fetchrow(
        "SELECT user_id FROM magic_link_tokens WHERE user_id = $1",
        row["id"],
    )
    assert token_row is not None, "Invite token row must be created in magic_link_tokens"


async def test_a5_invite_user_409_on_duplicate_email(admin_client, contract_conn):
    """Covers map row A5: inviting an already-existing email returns 409.

    Verified: admin.py:163-172 conflict check at HEAD.
    """
    dup_email = "dup-invite-contract@example.com"
    # Pre-insert the user directly.
    await contract_conn.execute(
        "INSERT INTO users (email, role) VALUES ($1, 'user')",
        dup_email,
    )

    resp = await admin_client.post(
        "/api/admin/users",
        json={"email": dup_email, "role": "user"},
    )

    assert resp.status_code == 409, (
        f"Expected 409 on duplicate email; got {resp.status_code}: {resp.text[:300]}"
    )


# ---------------------------------------------------------------------------
# A6: PATCH /api/admin/users/{id}/role — admin updates user role
# ---------------------------------------------------------------------------


async def test_a6_update_role_persists_to_db(admin_client, contract_conn):
    """Covers map row A6: role update writes to users.role in DB.

    Verified: admin.py:224-272 update_user_role at HEAD.
    Survivor-of: test_admin_users.py role-change mock assertions.
    """
    # Seed a target user.
    target_id = await contract_conn.fetchval(
        "INSERT INTO users (email, role) VALUES ($1, 'user') RETURNING id",
        "role-change-target@example.com",
    )

    resp = await admin_client.patch(
        f"/api/admin/users/{target_id}/role",
        json={"role": "admin"},
    )

    assert resp.status_code == 200, (
        f"Expected 200 from role update; got {resp.status_code}: {resp.text[:300]}"
    )
    body = resp.json()
    assert body["role"] == "admin"
    assert body["id"] == target_id

    # Verify the role landed in DB.
    db_role = await contract_conn.fetchval(
        "SELECT role FROM users WHERE id = $1",
        target_id,
    )
    assert db_role == "admin", f"Expected role='admin' in DB, got {db_role!r}"


async def test_a6_update_role_404_on_nonexistent_user(admin_client):
    """Covers map row A6: PATCH role for non-existent user returns 404.

    Verified: admin.py:261-262 row-is-None 404 at HEAD.
    """
    resp = await admin_client.patch(
        "/api/admin/users/999999999/role",
        json={"role": "admin"},
    )
    assert resp.status_code == 404, (
        f"Expected 404 for missing user; got {resp.status_code}: {resp.text[:300]}"
    )


# ---------------------------------------------------------------------------
# A7: DELETE /api/admin/users/{id} — admin soft-deletes a user
# ---------------------------------------------------------------------------


async def test_a7_soft_delete_sets_deleted_at(admin_client, contract_conn):
    """Covers map row A7: soft-delete sets users.deleted_at in DB.

    Verified: admin.py:281-321 soft_delete_user at HEAD.
    Survivor-of: test_admin_users.py delete mock assertions.
    """
    target_id = await contract_conn.fetchval(
        "INSERT INTO users (email, role) VALUES ($1, 'user') RETURNING id",
        "soft-delete-target@example.com",
    )

    resp = await admin_client.delete(f"/api/admin/users/{target_id}")

    assert resp.status_code == 204, (
        f"Expected 204 from soft_delete; got {resp.status_code}: {resp.text[:300]}"
    )

    # Verify deleted_at is now set.
    deleted_at = await contract_conn.fetchval(
        "SELECT deleted_at FROM users WHERE id = $1",
        target_id,
    )
    assert deleted_at is not None, "soft_delete must set deleted_at in DB"


async def test_a7_self_delete_returns_400(admin_client):
    """Covers map row A7: admin cannot delete their own account → 400.

    Verified: admin.py:289-293 self-delete guard at HEAD.
    """
    # The fixture seeds admin_user_id; retrieve the seeded admin id from the DB.
    admin_id = admin_client.admin_user_id  # type: ignore[attr-defined]
    resp = await admin_client.delete(f"/api/admin/users/{admin_id}")
    assert resp.status_code == 400, (
        f"Expected 400 when admin deletes self; got {resp.status_code}: {resp.text[:300]}"
    )


# ---------------------------------------------------------------------------
# A8: POST /api/admin/users/{id}/restore — admin restores a soft-deleted user
# ---------------------------------------------------------------------------


async def test_a8_restore_user_clears_deleted_at(admin_client, contract_conn):
    """Covers map row A8: restore clears users.deleted_at within 30-day grace.

    Verified: admin.py:329-364 restore_user at HEAD.
    Survivor-of: test_admin_user_deletion.py restore tests.
    """
    target_id = await contract_conn.fetchval(
        "INSERT INTO users (email, role) VALUES ($1, 'user') RETURNING id",
        "restore-target@example.com",
    )
    # Soft-delete the user in DB (within 30-day window).
    await contract_conn.execute(
        "UPDATE users SET deleted_at = NOW() - INTERVAL '1 hour' WHERE id = $1",
        target_id,
    )

    resp = await admin_client.post(f"/api/admin/users/{target_id}/restore")

    assert resp.status_code == 200, (
        f"Expected 200 from restore; got {resp.status_code}: {resp.text[:300]}"
    )
    body = resp.json()
    assert body["id"] == target_id

    # Verify deleted_at is cleared.
    deleted_at = await contract_conn.fetchval(
        "SELECT deleted_at FROM users WHERE id = $1",
        target_id,
    )
    assert deleted_at is None, "restore must clear deleted_at in DB"


async def test_a8_restore_outside_grace_returns_404(admin_client, contract_conn):
    """Covers map row A8: restore past 30-day grace returns 404.

    Verified: admin.py:339-355 grace-window check at HEAD.
    """
    target_id = await contract_conn.fetchval(
        "INSERT INTO users (email, role) VALUES ($1, 'user') RETURNING id",
        "restore-expired-target@example.com",
    )
    # Soft-delete the user past the 30-day grace window.
    await contract_conn.execute(
        "UPDATE users SET deleted_at = NOW() - INTERVAL '31 days' WHERE id = $1",
        target_id,
    )

    resp = await admin_client.post(f"/api/admin/users/{target_id}/restore")

    assert resp.status_code == 404, (
        f"Expected 404 for restore outside grace; got {resp.status_code}: {resp.text[:300]}"
    )


# ---------------------------------------------------------------------------
# A14: GET /api/admin/audit-log — admin views global audit log
# ---------------------------------------------------------------------------


async def test_a14_audit_log_returns_entries_from_db(audit_admin_client, contract_conn):
    """Covers map row A14: GET /api/admin/audit-log returns DB audit rows.

    Verified: audit_admin.py:37-84 list_audit_log at HEAD.
    Survivor-of: logs/audit mock-unit assertions.
    """
    # Insert a known audit_log row in the same transaction.
    await contract_conn.execute(
        "INSERT INTO audit_log (action, resource) VALUES ($1, $2)",
        "contract.test.action",
        "users/1",
    )

    resp = await audit_admin_client.get("/api/admin/audit-log")

    assert resp.status_code == 200, (
        f"Expected 200 from audit-log; got {resp.status_code}: {resp.text[:300]}"
    )
    body = resp.json()
    assert "entries" in body, f"Response must have 'entries' key; got: {body}"
    # The inserted row must appear.
    actions = [e["action"] for e in body["entries"]]
    assert "contract.test.action" in actions, f"Inserted audit row not found in entries: {actions}"


async def test_a14_audit_log_non_admin_gets_403(plain_client):
    """Covers map row A14: non-admin (role='user') gets 403 on audit-log endpoint.

    audit_admin.py uses jarvis_common.auth.require_admin which requires role='admin'.
    The plain_client has a valid session but role='user'.

    Verified: jarvis_common/auth.py:193-214 require_admin at HEAD.
    """
    resp = await plain_client.get("/api/admin/audit-log")
    assert resp.status_code == 403, (
        f"Expected 403 for non-admin on audit-log; got {resp.status_code}: {resp.text[:300]}"
    )
