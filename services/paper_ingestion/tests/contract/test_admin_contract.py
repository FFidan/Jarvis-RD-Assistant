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

# Verified: services/paper_ingestion/paper_ingestion/routers/admin.py:263 update_user_role,
#           :331 soft_delete_user — last-administrator guard serialized via
#           pg_advisory_xact_lock(hashtext('admin_role_mutation')) inside conn.transaction().

from __future__ import annotations

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch
from jarvis_common.testing import SharedConnPool

from paper_ingestion.routers.audit_admin import _build_audit_query

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


async def _set_database_owner(conn, user_id: int) -> None:
    from jarvis_common.owner import OWNER_USER_ID_CONFIG_KEY

    await conn.execute(
        """
        INSERT INTO user_config (user_id, key, value)
        VALUES (NULL, $1, to_jsonb($2::bigint))
        """,
        OWNER_USER_ID_CONFIG_KEY,
        user_id,
    )


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


async def test_a4_list_users_marks_only_the_valid_instance_owner(admin_client, contract_conn):
    owner_id = admin_client.admin_user_id  # type: ignore[attr-defined]
    member_id = await contract_conn.fetchval(
        "INSERT INTO users (email, role) VALUES ($1, 'user') RETURNING id",
        "owner-list-member@example.com",
    )
    await _set_database_owner(contract_conn, owner_id)

    response = await admin_client.get("/api/admin/users")

    assert response.status_code == 200, response.text
    records = {record["id"]: record for record in response.json()}
    assert records[owner_id]["is_owner"] is True
    assert records[member_id]["is_owner"] is False
    assert {record["owner_source"] for record in records.values()} == {"database"}
    assert {record["owner_state"] for record in records.values()} == {"valid"}


async def test_a4_list_users_non_admin_gets_403(plain_client):
    """Covers map row A4: non-admin caller gets 403 from admin role gate.

    Verified: admin.py:78-91 require_admin at HEAD.
    """
    resp = await plain_client.get("/api/admin/users")
    assert resp.status_code == 403, (
        f"Expected 403 for non-admin on /api/admin/users; got {resp.status_code}"
    )


async def test_a4_include_deleted_surfaces_restorable_soft_deleted_users(
    admin_client, contract_conn
):
    """Covers map row A4: ``include_deleted=true`` surfaces restorable soft-deleted
    users (non-null ``deleted_at``) so the admin UI can restore them; the default
    list omits them.

    Verified: admin.py list_users include_deleted branch at HEAD.
    """
    target_id = await contract_conn.fetchval(
        "INSERT INTO users (email, role) VALUES ($1, 'user') RETURNING id",
        "include-deleted-target@example.com",
    )
    await contract_conn.execute(
        "UPDATE users SET deleted_at = NOW() - INTERVAL '1 hour' WHERE id = $1",
        target_id,
    )

    # Default list must omit the soft-deleted user.
    default_resp = await admin_client.get("/api/admin/users")
    assert default_resp.status_code == 200, (
        f"Expected 200 from default list; got {default_resp.status_code}: {default_resp.text[:300]}"
    )
    assert target_id not in {u["id"] for u in default_resp.json()}, (
        "Default list must omit soft-deleted users"
    )

    # include_deleted=true must surface it with a non-null deleted_at.
    resp = await admin_client.get("/api/admin/users?include_deleted=true")
    assert resp.status_code == 200, (
        f"Expected 200 from include_deleted list; got {resp.status_code}: {resp.text[:300]}"
    )
    deleted_row = next((u for u in resp.json() if u["id"] == target_id), None)
    assert deleted_row is not None, "include_deleted=true must surface the soft-deleted user"
    assert deleted_row["deleted_at"] is not None, (
        "soft-deleted row must carry a non-null deleted_at"
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


async def test_a5_invite_soft_deleted_email_returns_409(admin_client, contract_conn):
    """Covers map row A5: inviting an email that belongs to a soft-deleted user returns 409.

    Before the fix this raised asyncpg.UniqueViolationError → 500 because the
    pre-check only guards against non-deleted duplicates (deleted_at IS NULL).
    The unique constraint on users.email covers soft-deleted rows too.

    Verified: admin.py:144-152 INSERT fetchrow + fix at HEAD.
    """
    soft_deleted_email = "soft-deleted-invite-contract@example.com"
    # Insert a user and then soft-delete them.
    user_id = await contract_conn.fetchval(
        "INSERT INTO users (email, role) VALUES ($1, 'user') RETURNING id",
        soft_deleted_email,
    )
    await contract_conn.execute(
        "UPDATE users SET deleted_at = NOW() WHERE id = $1",
        user_id,
    )

    resp = await admin_client.post(
        "/api/admin/users",
        json={"email": soft_deleted_email, "role": "user"},
    )

    assert resp.status_code == 409, (
        f"Expected 409 for soft-deleted email invite; got {resp.status_code}: {resp.text[:300]}"
    )
    assert "removed" in resp.json().get("detail", "").lower(), (
        f"Expected detail to mention 'removed'; got: {resp.json()}"
    )


async def test_invite_user_token_insert_failure_rolls_back_user(admin_client, contract_conn):
    """Users row must be absent when the magic_link_tokens INSERT fails.

    Verifies atomicity: both the users INSERT and the magic_link_tokens INSERT
    must share one transaction so a token failure cannot leave an orphan user.
    """
    from jarvis_common.testing import SharedConnPool
    from paper_ingestion.main import app

    email = "atomicity-invite@example.com"

    class _FailingConn:
        """Wraps contract_conn but raises on the magic_link_tokens INSERT."""

        async def execute(self, query, *args, **kwargs):
            if "magic_link_tokens" in query:
                raise RuntimeError("Simulated token INSERT failure")
            return await contract_conn.execute(query, *args, **kwargs)

        def transaction(self):
            return contract_conn.transaction()

        async def fetchrow(self, *args, **kwargs):
            return await contract_conn.fetchrow(*args, **kwargs)

        async def fetchval(self, *args, **kwargs):
            return await contract_conn.fetchval(*args, **kwargs)

        async def fetch(self, *args, **kwargs):
            return await contract_conn.fetch(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(contract_conn, name)

    failing_pool = SharedConnPool(_FailingConn())
    original_pool = app.state.db_pool
    app.state.db_pool = failing_pool
    try:
        # The unhandled RuntimeError propagates through the httpx ASGI transport
        # in test mode — catching it is expected; the key assertion is row state.
        await admin_client.post(
            "/api/admin/users",
            json={"email": email, "role": "user"},
        )
    except RuntimeError:
        pass
    finally:
        app.state.db_pool = original_pool

    row = await contract_conn.fetchrow(
        "SELECT id FROM users WHERE email = $1",
        email,
    )
    assert row is None, "users row must be rolled back when token INSERT fails"


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


async def test_promoting_configured_non_admin_owner_requires_explicit_repair(
    admin_client, contract_conn
):
    target_id = await contract_conn.fetchval(
        "INSERT INTO users (email, role) VALUES ($1, 'user') RETURNING id",
        "configured-member-owner@example.com",
    )
    await _set_database_owner(contract_conn, target_id)

    response = await admin_client.patch(
        f"/api/admin/users/{target_id}/role",
        json={"role": "admin"},
    )

    assert response.status_code == 409, response.text
    assert "owner" in response.json()["detail"].lower()
    persisted_role = await contract_conn.fetchval("SELECT role FROM users WHERE id = $1", target_id)
    assert persisted_role == "user"


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
# ≥1-admin invariant — the last admin can't be demoted or deleted.
# Both endpoints serialise the recheck on pg_advisory_xact_lock('admin_role_mutation')
# acquired INSIDE an open conn.transaction().
# ---------------------------------------------------------------------------


async def test_demote_last_admin_returns_400_and_keeps_role(admin_client, contract_conn):
    """Demoting the sole surviving admin is refused with 400; the role is unchanged.

    Regression proof: remove the last-admin guard in update_user_role and this
    demotion succeeds (200) with the DB role flipped to 'user'.

    Verified: admin.py update_user_role last-admin guard at HEAD.
    """
    admin_id = admin_client.admin_user_id  # type: ignore[attr-defined]

    resp = await admin_client.patch(
        f"/api/admin/users/{admin_id}/role",
        json={"role": "user"},
    )

    assert resp.status_code == 400, (
        f"Expected 400 demoting the last admin; got {resp.status_code}: {resp.text[:300]}"
    )
    db_role = await contract_conn.fetchval("SELECT role FROM users WHERE id = $1", admin_id)
    assert db_role == "admin", (
        f"Last admin must stay admin after a blocked demotion; got {db_role!r}"
    )


async def test_demote_non_last_admin_succeeds(admin_client, contract_conn):
    """With more than one admin, demoting another admin is allowed (cross-admin path).

    Verified: admin.py update_user_role last-admin guard at HEAD.
    """
    second_admin_id = await contract_conn.fetchval(
        "INSERT INTO users (email, role) VALUES ($1, 'admin') RETURNING id",
        "second-admin-demote@example.com",
    )

    resp = await admin_client.patch(
        f"/api/admin/users/{second_admin_id}/role",
        json={"role": "user"},
    )

    assert resp.status_code == 200, (
        f"Expected 200 demoting a non-last admin; got {resp.status_code}: {resp.text[:300]}"
    )
    db_role = await contract_conn.fetchval("SELECT role FROM users WHERE id = $1", second_admin_id)
    assert db_role == "user", f"Demoted admin must be 'user' in DB; got {db_role!r}"


async def test_configured_owner_cannot_be_demoted_before_transfer(admin_client, contract_conn):
    owner_id = await contract_conn.fetchval(
        "INSERT INTO users (email, role) VALUES ($1, 'admin') RETURNING id",
        "protected-owner-demote@example.com",
    )
    await _set_database_owner(contract_conn, owner_id)

    response = await admin_client.patch(
        f"/api/admin/users/{owner_id}/role",
        json={"role": "user"},
    )

    assert response.status_code == 409, response.text
    assert "transfer ownership" in response.json()["detail"].lower()
    persisted_role = await contract_conn.fetchval("SELECT role FROM users WHERE id = $1", owner_id)
    assert persisted_role == "admin"


async def test_delete_last_admin_returns_400_and_keeps_row(admin_client, contract_conn):
    """Deleting the sole surviving admin is refused with 400; the row is not soft-deleted.

    Verified: admin.py soft_delete_user self + last-admin guards at HEAD.
    """
    admin_id = admin_client.admin_user_id  # type: ignore[attr-defined]

    resp = await admin_client.delete(f"/api/admin/users/{admin_id}")

    assert resp.status_code == 400, (
        f"Expected 400 deleting the last admin; got {resp.status_code}: {resp.text[:300]}"
    )
    deleted_at = await contract_conn.fetchval(
        "SELECT deleted_at FROM users WHERE id = $1", admin_id
    )
    assert deleted_at is None, "Last admin must not be soft-deleted after a blocked delete"


async def test_delete_non_last_admin_succeeds(admin_client, contract_conn):
    """With more than one admin, soft-deleting another admin is allowed (cross-admin path).

    Verified: admin.py soft_delete_user last-admin guard at HEAD.
    """
    second_admin_id = await contract_conn.fetchval(
        "INSERT INTO users (email, role) VALUES ($1, 'admin') RETURNING id",
        "second-admin-delete@example.com",
    )

    resp = await admin_client.delete(f"/api/admin/users/{second_admin_id}")

    assert resp.status_code == 204, (
        f"Expected 204 deleting a non-last admin; got {resp.status_code}: {resp.text[:300]}"
    )
    deleted_at = await contract_conn.fetchval(
        "SELECT deleted_at FROM users WHERE id = $1", second_admin_id
    )
    assert deleted_at is not None, "Non-last admin must be soft-deleted in DB"


async def test_configured_owner_cannot_be_deleted_before_transfer(admin_client, contract_conn):
    owner_id = await contract_conn.fetchval(
        "INSERT INTO users (email, role) VALUES ($1, 'admin') RETURNING id",
        "protected-owner-delete@example.com",
    )
    await _set_database_owner(contract_conn, owner_id)

    response = await admin_client.delete(f"/api/admin/users/{owner_id}")

    assert response.status_code == 409, response.text
    assert "transfer ownership" in response.json()["detail"].lower()
    assert (
        await contract_conn.fetchval("SELECT deleted_at FROM users WHERE id = $1", owner_id) is None
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


async def test_restoring_configured_deleted_admin_owner_requires_explicit_repair(
    admin_client, contract_conn, monkeypatch
):
    monkeypatch.setenv("JARVIS_MODEL_HMAC_KEY", "x" * 32)
    target_id = await contract_conn.fetchval(
        "INSERT INTO users (email, role) VALUES ($1, 'admin') RETURNING id",
        "configured-deleted-owner@example.com",
    )
    await contract_conn.execute("UPDATE users SET deleted_at = NOW() WHERE id = $1", target_id)
    await _set_database_owner(contract_conn, target_id)

    response = await admin_client.post(f"/api/admin/users/{target_id}/restore")

    assert response.status_code == 409, response.text
    assert "owner" in response.json()["detail"].lower()
    assert (
        await contract_conn.fetchval("SELECT deleted_at FROM users WHERE id = $1", target_id)
        is not None
    )


# ---------------------------------------------------------------------------
# Instance-owner transfer and strict owner-mutation audit
# ---------------------------------------------------------------------------


async def test_owner_transfer_is_atomic_audited_and_not_replayable(admin_client, contract_conn):
    current_owner_id = admin_client.admin_user_id  # type: ignore[attr-defined]
    target_email = "next-owner@example.com"
    target_id = await contract_conn.fetchval(
        "INSERT INTO users (email, role) VALUES ($1, 'admin') RETURNING id",
        target_email,
    )
    await _set_database_owner(contract_conn, current_owner_id)

    response = await admin_client.post(
        "/api/admin/owner/transfer",
        json={"target_user_id": target_id, "confirmation": target_email},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "source": "database",
        "state": "valid",
        "user_id": target_id,
    }
    stored_owner = await contract_conn.fetchval(
        "SELECT value #>> '{}' FROM user_config WHERE user_id IS NULL AND key = 'owner.user_id'"
    )
    assert stored_owner == str(target_id)
    audit = await contract_conn.fetchrow(
        "SELECT user_id, action, resource, metadata FROM audit_log "
        "WHERE action = 'admin.owner.transfer' ORDER BY id DESC LIMIT 1"
    )
    assert audit is not None
    assert audit["user_id"] == str(current_owner_id)
    assert audit["resource"] == "owner.user_id"
    assert audit["metadata"]["previous_owner_user_id"] == current_owner_id
    assert audit["metadata"]["new_owner_user_id"] == target_id

    replay = await admin_client.post(
        "/api/admin/owner/transfer",
        json={"target_user_id": target_id, "confirmation": target_email},
    )
    assert replay.status_code == 403, replay.text


async def test_owner_transfer_rejects_environment_managed_owner(
    admin_client, contract_conn, monkeypatch
):
    current_owner_id = admin_client.admin_user_id  # type: ignore[attr-defined]
    target_id = await contract_conn.fetchval(
        "INSERT INTO users (email, role) VALUES ($1, 'admin') RETURNING id",
        "environment-transfer-target@example.com",
    )
    monkeypatch.setenv("OWNER_USER_ID", str(current_owner_id))

    response = await admin_client.post(
        "/api/admin/owner/transfer",
        json={
            "target_user_id": target_id,
            "confirmation": "environment-transfer-target@example.com",
        },
    )

    assert response.status_code == 409, response.text
    assert "OWNER_USER_ID" in response.json()["detail"]


async def test_owner_transfer_rejects_non_owner_caller(admin_client, contract_conn):
    configured_owner_id = await contract_conn.fetchval(
        "INSERT INTO users (email, role) VALUES ($1, 'admin') RETURNING id",
        "configured-owner@example.com",
    )
    target_id = await contract_conn.fetchval(
        "INSERT INTO users (email, role) VALUES ($1, 'admin') RETURNING id",
        "non-owner-transfer-target@example.com",
    )
    await _set_database_owner(contract_conn, configured_owner_id)

    response = await admin_client.post(
        "/api/admin/owner/transfer",
        json={
            "target_user_id": target_id,
            "confirmation": "non-owner-transfer-target@example.com",
        },
    )

    assert response.status_code == 403, response.text


async def test_owner_transfer_validates_target_and_confirmation(admin_client, contract_conn):
    current_owner_id = admin_client.admin_user_id  # type: ignore[attr-defined]
    member_id = await contract_conn.fetchval(
        "INSERT INTO users (email, role) VALUES ($1, 'user') RETURNING id",
        "member-transfer-target@example.com",
    )
    await _set_database_owner(contract_conn, current_owner_id)

    non_admin = await admin_client.post(
        "/api/admin/owner/transfer",
        json={
            "target_user_id": member_id,
            "confirmation": "member-transfer-target@example.com",
        },
    )
    assert non_admin.status_code == 400, non_admin.text

    target_id = await contract_conn.fetchval(
        "INSERT INTO users (email, role) VALUES ($1, 'admin') RETURNING id",
        "confirmed-transfer-target@example.com",
    )
    mismatch = await admin_client.post(
        "/api/admin/owner/transfer",
        json={"target_user_id": target_id, "confirmation": "wrong@example.com"},
    )
    assert mismatch.status_code == 400, mismatch.text

    self_transfer = await admin_client.post(
        "/api/admin/owner/transfer",
        json={
            "target_user_id": current_owner_id,
            "confirmation": "admin-contract-test@example.com",
        },
    )
    assert self_transfer.status_code == 400, self_transfer.text


async def test_owner_transfer_rolls_back_when_strict_audit_fails(admin_client, contract_conn):
    current_owner_id = admin_client.admin_user_id  # type: ignore[attr-defined]
    target_email = "audit-failure-transfer@example.com"
    target_id = await contract_conn.fetchval(
        "INSERT INTO users (email, role) VALUES ($1, 'admin') RETURNING id",
        target_email,
    )
    await _set_database_owner(contract_conn, current_owner_id)

    with (
        patch(
            "paper_ingestion.routers.admin.log_audit_strict",
            new=AsyncMock(side_effect=RuntimeError("audit unavailable")),
        ),
        pytest.raises(RuntimeError, match="audit unavailable"),
    ):
        await admin_client.post(
            "/api/admin/owner/transfer",
            json={"target_user_id": target_id, "confirmation": target_email},
        )

    stored_owner = await contract_conn.fetchval(
        "SELECT value #>> '{}' FROM user_config WHERE user_id IS NULL AND key = 'owner.user_id'"
    )
    assert stored_owner == str(current_owner_id)


@pytest.mark.parametrize("operation", ["role", "delete"])
async def test_owner_sensitive_user_mutation_rolls_back_when_strict_audit_fails(
    operation, admin_client, contract_conn
):
    target_id = await contract_conn.fetchval(
        "INSERT INTO users (email, role) VALUES ($1, 'admin') RETURNING id",
        f"audit-failure-{operation}@example.com",
    )

    with (
        patch(
            "paper_ingestion.routers.admin.log_audit_strict",
            new=AsyncMock(side_effect=RuntimeError("audit unavailable")),
        ),
        pytest.raises(RuntimeError, match="audit unavailable"),
    ):
        if operation == "role":
            await admin_client.patch(
                f"/api/admin/users/{target_id}/role",
                json={"role": "user"},
            )
        else:
            await admin_client.delete(f"/api/admin/users/{target_id}")

    row = await contract_conn.fetchrow(
        "SELECT role, deleted_at FROM users WHERE id = $1", target_id
    )
    assert row["role"] == "admin"
    assert row["deleted_at"] is None


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


# ---------------------------------------------------------------------------
# _build_audit_query unit tests — pure-function, no DB required
# ---------------------------------------------------------------------------

_AUDIT_COLS = (
    'SELECT id, user_id, action, resource, metadata, "timestamp" AS created_at FROM audit_log '
)


async def test_build_audit_query_no_filters():
    """No filters: a single LIMIT placeholder and no user-supplied bound values."""
    sql, params = _build_audit_query(None, None, 50)
    assert params == [51]
    assert sql.count("$") == 1
    assert sql.startswith(_AUDIT_COLS)


async def test_build_audit_query_before_id_only():
    """before_id is bound as a param (never interpolated); LIMIT is the second placeholder."""
    sql, params = _build_audit_query(100, None, 50)
    assert params == [100, 51]
    assert sql.count("$") == 2
    assert sql.startswith(_AUDIT_COLS)


async def test_build_audit_query_action_prefix_only():
    """action_prefix is escaped and bound as a param; LIMIT is the second placeholder."""
    sql, params = _build_audit_query(None, "user.login", 10)
    assert params == ["user.login%", 11]
    assert sql.count("$") == 2


async def test_build_audit_query_both_filters():
    """Both filters bind in order: before_id, action_prefix, then LIMIT."""
    sql, params = _build_audit_query(100, "user.login", 10)
    assert params == [100, "user.login%", 11]
    assert sql.count("$") == 3


async def test_build_audit_query_action_prefix_special_chars_are_escaped():
    """%, _, and backslash in action_prefix are LIKE-escaped in the bound param."""
    sql, params = _build_audit_query(None, "admin%_op\\x", 5)
    # Each special char must be escaped with a leading backslash; the value is a
    # bound param, so the placeholder count is unchanged by its content.
    assert params[0] == "admin\\%\\_op\\\\x%"
    assert params[1] == 6
    assert sql.count("$") == 2
