"""Deleting an account signs it out — and signs out nobody else.

An unexpired session cookie would otherwise keep authenticating a soft-deleted
account, so the revocation runs inside the same transaction as the delete. The
scoping is the part that needs a real database: a revocation missing its
user-id predicate would sign out every account on the instance.
"""

# Verified: services/paper_ingestion/paper_ingestion/routers/admin.py:561
# (soft_delete_user revokes sessions inside the delete transaction)
# Verified: services/paper_ingestion/paper_ingestion/routers/admin.py:586
# (restore_user clears deleted_at only; revocation is permanent)

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from jarvis_common.testing import SharedConnPool

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]


async def _seed_user_with_session(conn, email: str, role: str) -> tuple[int, str]:
    """Insert a user plus one live session; return (user_id, session_id)."""
    user_id = await conn.fetchval(
        "INSERT INTO users (email, role) VALUES ($1, $2) RETURNING id", email, role
    )
    session_id = await conn.fetchval(
        """INSERT INTO sessions (user_id, expires_at)
           VALUES ($1, NOW() + INTERVAL '1 day') RETURNING id""",
        user_id,
    )
    return int(user_id), str(session_id)


async def _revoked_at(conn, session_id: str):
    return await conn.fetchval("SELECT revoked_at FROM sessions WHERE id = $1::uuid", session_id)


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def admin_client(contract_conn):
    """ASGI client signed in as an admin through a real session cookie."""
    from jarvis_common import verify_api_key
    from jarvis_common.testing_contract_apps import (
        make_contract_client,
        patch_app_state,
        patch_dependency_overrides,
    )
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app

    _admin_id, admin_cookie = await _seed_user_with_session(
        contract_conn, "session-revocation-admin@example.com", "admin"
    )
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
                yield client
    finally:
        app.state.limiter.enabled = True


async def test_soft_delete_revokes_the_targets_sessions_and_no_others(admin_client, contract_conn):
    """Deleting one account leaves every other account signed in."""
    target_id, target_session = await _seed_user_with_session(
        contract_conn, "revocation-target@example.com", "user"
    )
    bystander_id, bystander_session = await _seed_user_with_session(
        contract_conn, "revocation-bystander@example.com", "user"
    )

    resp = await admin_client.delete(f"/api/admin/users/{target_id}")

    assert resp.status_code == 204, resp.text[:300]
    assert await _revoked_at(contract_conn, target_session) is not None, (
        "the deleted account's session must be revoked"
    )
    assert await _revoked_at(contract_conn, bystander_session) is None, (
        f"user {bystander_id} was signed out by another account's deletion"
    )


async def test_restore_does_not_resurrect_revoked_sessions(admin_client, contract_conn):
    """Revocation is permanent — a restored user signs in again, not back in."""
    target_id, target_session = await _seed_user_with_session(
        contract_conn, "revocation-restore-target@example.com", "user"
    )

    delete_resp = await admin_client.delete(f"/api/admin/users/{target_id}")
    assert delete_resp.status_code == 204, delete_resp.text[:300]
    revoked_at = await _revoked_at(contract_conn, target_session)
    assert revoked_at is not None, "precondition: the delete must have revoked the session"

    restore_resp = await admin_client.post(f"/api/admin/users/{target_id}/restore")

    assert restore_resp.status_code == 200, restore_resp.text[:300]
    assert await _revoked_at(contract_conn, target_session) == revoked_at, (
        "restore must not reinstate a revoked session"
    )
