"""Projects CRUD contract tests — A213, A215, A216.

Covers:
- POST /api/projects   (A213) — row inserted with caller's user_id; absent from user B's list
- PUT /api/projects/id (A215) — update for owner; 404 for non-owner
- DELETE /api/projects/id (A216) — deleted for owner; 404 for non-owner
"""

from __future__ import annotations

import pytest


from jarvis_common.testing_contract_apps import (
    make_contract_client as _client,
)

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]


# ---------------------------------------------------------------------------
# §A213 — POST /api/projects — row inserted with caller's user_id
# ---------------------------------------------------------------------------


async def test_create_project_row_has_caller_user_id(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """POST /api/projects creates a row with the caller's user_id in DB.

    Collapses test_routers_coverage.py's mock-pool assertion to a real
    INSERT+SELECT proof that user_id is correctly set on the persisted row.
    """
    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp = await c.post("/api/projects", json={"name": "Contract Project Alpha"})

    assert resp.status_code == 201, (
        f"POST /api/projects failed: {resp.status_code}: {resp.text[:300]}"
    )
    body = resp.json()
    project_id = body["id"]

    # Verify the row in DB belongs to user A
    db_user_id = await contract_conn.fetchval(
        "SELECT user_id FROM projects WHERE id = $1",
        project_id,
    )
    assert db_user_id == contract_two_users.user_a_id, (
        f"Project {project_id} has user_id={db_user_id} in DB; "
        f"expected user_a_id={contract_two_users.user_a_id}"
    )


async def test_create_project_absent_from_user_b_list(
    contract_two_users, _le_app, _configure_api_key
):
    """Project created by user A does not appear in user B's list.

    Exercises the WHERE p.user_id = $1 scoping filter in list_projects
    against a freshly created row.
    """
    async with _client(_le_app, contract_two_users.cookie_a) as c:
        create_resp = await c.post("/api/projects", json={"name": "Contract Project Beta"})
    assert create_resp.status_code == 201
    new_id = create_resp.json()["id"]

    async with _client(_le_app, contract_two_users.cookie_b) as c:
        list_resp = await c.get("/api/projects")
    assert list_resp.status_code == 200
    b_ids = [p["id"] for p in list_resp.json()]
    assert new_id not in b_ids, (
        f"IDOR: user B sees user A's newly created project {new_id} in list {b_ids}"
    )


# ---------------------------------------------------------------------------
# §A215 — PUT /api/projects/{id} — owner can update; non-owner gets 404
# ---------------------------------------------------------------------------


async def test_update_project_owner_gets_200(contract_two_users, _le_app, _configure_api_key):
    """User A can update their own project — 200 with updated fields.

    Exercises the real ``SELECT * FROM projects WHERE id = $1 AND user_id = $2
    FOR UPDATE`` ownership check.
    """
    project_id = contract_two_users.project_id_a
    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp = await c.put(
            f"/api/projects/{project_id}",
            json={"name": "Updated Project Name"},
        )

    assert resp.status_code == 200, (
        f"Owner expected 200 updating project {project_id}; "
        f"got {resp.status_code}: {resp.text[:300]}"
    )
    assert resp.json()["name"] == "Updated Project Name"


async def test_update_project_user_b_gets_404(contract_two_users, _le_app, _configure_api_key):
    """User B cannot update user A's project — must get 404 (IDOR guard).

    Collapses test_le_hardening.py's mock-pool assertion to a real scoping proof.
    """
    project_id = contract_two_users.project_id_a
    async with _client(_le_app, contract_two_users.cookie_b) as c:
        resp = await c.put(
            f"/api/projects/{project_id}",
            json={"name": "Hijacked Name"},
        )

    assert resp.status_code != 401, f"PUT /api/projects/{project_id}: got 401 — session wiring bug"
    assert resp.status_code == 404, (
        f"IDOR: user B got {resp.status_code} updating user A's project {project_id} "
        f"(expected 404). Body: {resp.text[:300]}"
    )


# ---------------------------------------------------------------------------
# §A216 — DELETE /api/projects/{id} — owner can delete; non-owner gets 404
# ---------------------------------------------------------------------------


async def test_delete_project_user_b_gets_404(contract_two_users, _le_app, _configure_api_key):
    """User B cannot delete user A's project — must get 404.

    Exercises the real ``DELETE FROM projects WHERE id = $1 AND user_id = $2``
    ownership filter.
    """
    project_id = contract_two_users.project_id_a
    async with _client(_le_app, contract_two_users.cookie_b) as c:
        resp = await c.delete(f"/api/projects/{project_id}")

    assert resp.status_code != 401, (
        f"DELETE /api/projects/{project_id}: got 401 — session wiring bug"
    )
    assert resp.status_code == 404, (
        f"IDOR: user B got {resp.status_code} trying to delete user A's project {project_id} "
        f"(expected 404). Body: {resp.text[:300]}"
    )


async def test_delete_project_owner_gets_204(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """User A can delete their own project — 204 and row gone from DB."""
    # Create a fresh project to delete (don't destroy the fixture's project_id_a)
    new_id = await contract_conn.fetchval(
        "INSERT INTO projects (name, user_id) VALUES ('Deletable Project', $1) RETURNING id",
        contract_two_users.user_a_id,
    )
    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp = await c.delete(f"/api/projects/{new_id}")

    assert resp.status_code == 204, (
        f"Owner expected 204 deleting project {new_id}; got {resp.status_code}: {resp.text[:300]}"
    )
    still_exists = await contract_conn.fetchval(
        "SELECT id FROM projects WHERE id = $1",
        new_id,
    )
    assert still_exists is None, f"Project {new_id} still exists in DB after DELETE 204"
