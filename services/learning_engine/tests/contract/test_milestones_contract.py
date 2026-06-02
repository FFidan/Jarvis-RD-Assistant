"""Milestones CRUD contract tests — A201, A202, A203, A204.

Covers:
- GET /api/projects/{id}/milestones    (A201) — owner sees list; non-owner 404
- POST /api/projects/{id}/milestones   (A202) — row inserted with user_id; non-owner 404
- PUT /api/milestones/{id}             (A203) — owner update; non-owner 404
- DELETE /api/milestones/{id}          (A204) — owner delete; non-owner 404
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
# §A201 — GET /api/projects/{id}/milestones
# ---------------------------------------------------------------------------


async def test_list_milestones_non_owner_gets_404(contract_two_users, _le_app, _configure_api_key):
    """User B cannot list milestones for user A's project — 404 (IDOR guard)."""
    project_id = contract_two_users.project_id_a
    async with _client(_le_app, contract_two_users.cookie_b) as c:
        resp = await c.get(f"/api/projects/{project_id}/milestones")

    assert resp.status_code == 404, (
        f"IDOR: user B got {resp.status_code} listing milestones for user A's project "
        f"{project_id} (expected 404). Body: {resp.text[:300]}"
    )


async def test_list_milestones_owner_sees_own(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """User A can list milestones for their own project (positive control)."""
    project_id = contract_two_users.project_id_a
    # Seed a milestone
    ms_id = await contract_conn.fetchval(
        """INSERT INTO milestones (project_id, name, deadline, user_id)
           VALUES ($1, 'Contract Milestone', NOW() + INTERVAL '7 days', $2)
           RETURNING id""",
        project_id,
        contract_two_users.user_a_id,
    )

    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp = await c.get(f"/api/projects/{project_id}/milestones")

    assert resp.status_code == 200, (
        f"GET milestones for owner failed: {resp.status_code}: {resp.text[:300]}"
    )
    ids = [m["id"] for m in resp.json()]
    assert ms_id in ids, f"Seeded milestone {ms_id} not in owner's list {ids}"


# ---------------------------------------------------------------------------
# §A202 — POST /api/projects/{id}/milestones
# ---------------------------------------------------------------------------


async def test_create_milestone_row_has_user_id(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """POST milestone creates row with correct user_id in DB."""
    project_id = contract_two_users.project_id_a
    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp = await c.post(
            f"/api/projects/{project_id}/milestones",
            json={"name": "New Contract MS", "deadline": "2099-12-31T00:00:00Z"},
        )

    assert resp.status_code == 201, f"POST milestone failed: {resp.status_code}: {resp.text[:300]}"
    ms_id = resp.json()["id"]
    db_user_id = await contract_conn.fetchval(
        "SELECT user_id FROM milestones WHERE id = $1",
        ms_id,
    )
    assert db_user_id == contract_two_users.user_a_id, (
        f"Milestone {ms_id} has user_id={db_user_id}; expected {contract_two_users.user_a_id}"
    )


async def test_create_milestone_non_owner_project_gets_404(
    contract_two_users, _le_app, _configure_api_key
):
    """User B cannot create a milestone in user A's project — 404."""
    project_id = contract_two_users.project_id_a
    async with _client(_le_app, contract_two_users.cookie_b) as c:
        resp = await c.post(
            f"/api/projects/{project_id}/milestones",
            json={"name": "Injected MS", "deadline": "2099-01-01T00:00:00Z"},
        )

    assert resp.status_code == 404, (
        f"IDOR: user B got {resp.status_code} creating milestone in user A's project "
        f"(expected 404). Body: {resp.text[:300]}"
    )


# ---------------------------------------------------------------------------
# §A203 — PUT /api/milestones/{id}
# ---------------------------------------------------------------------------


async def test_update_milestone_owner_gets_200(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """User A can update their own milestone — 200 with updated name."""
    ms_id = await contract_conn.fetchval(
        """INSERT INTO milestones (project_id, name, deadline, user_id)
           VALUES ($1, 'Original MS', NOW() + INTERVAL '30 days', $2)
           RETURNING id""",
        contract_two_users.project_id_a,
        contract_two_users.user_a_id,
    )

    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp = await c.put(f"/api/milestones/{ms_id}", json={"name": "Updated MS Name"})

    assert resp.status_code == 200, (
        f"Owner expected 200 updating milestone {ms_id}; got {resp.status_code}: {resp.text[:300]}"
    )
    assert resp.json()["name"] == "Updated MS Name"


async def test_update_milestone_user_b_gets_404(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """User B cannot update user A's milestone — 404 (IDOR guard)."""
    ms_id = await contract_conn.fetchval(
        """INSERT INTO milestones (project_id, name, deadline, user_id)
           VALUES ($1, 'B Tries To Edit', NOW() + INTERVAL '30 days', $2)
           RETURNING id""",
        contract_two_users.project_id_a,
        contract_two_users.user_a_id,
    )

    async with _client(_le_app, contract_two_users.cookie_b) as c:
        resp = await c.put(f"/api/milestones/{ms_id}", json={"name": "Hijacked"})

    assert resp.status_code != 401, f"PUT /api/milestones/{ms_id}: got 401 — session wiring bug"
    assert resp.status_code == 404, (
        f"IDOR: user B got {resp.status_code} updating user A's milestone {ms_id} "
        f"(expected 404). Body: {resp.text[:300]}"
    )


# ---------------------------------------------------------------------------
# §A204 — DELETE /api/milestones/{id}
# ---------------------------------------------------------------------------


async def test_delete_milestone_user_b_gets_404(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """User B cannot delete user A's milestone — 404 (IDOR guard)."""
    ms_id = await contract_conn.fetchval(
        """INSERT INTO milestones (project_id, name, deadline, user_id)
           VALUES ($1, 'B Tries To Delete', NOW() + INTERVAL '30 days', $2)
           RETURNING id""",
        contract_two_users.project_id_a,
        contract_two_users.user_a_id,
    )

    async with _client(_le_app, contract_two_users.cookie_b) as c:
        resp = await c.delete(f"/api/milestones/{ms_id}")

    assert resp.status_code != 401, f"DELETE /api/milestones/{ms_id}: got 401 — session wiring bug"
    assert resp.status_code == 404, (
        f"IDOR: user B got {resp.status_code} deleting user A's milestone {ms_id} "
        f"(expected 404). Body: {resp.text[:300]}"
    )


async def test_delete_milestone_owner_gets_204(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """User A can delete their own milestone — 204 and row gone from DB."""
    ms_id = await contract_conn.fetchval(
        """INSERT INTO milestones (project_id, name, deadline, user_id)
           VALUES ($1, 'Deletable MS', NOW() + INTERVAL '30 days', $2)
           RETURNING id""",
        contract_two_users.project_id_a,
        contract_two_users.user_a_id,
    )

    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp = await c.delete(f"/api/milestones/{ms_id}")

    assert resp.status_code == 204, (
        f"Owner expected 204 deleting milestone {ms_id}; got {resp.status_code}: {resp.text[:300]}"
    )
    still_exists = await contract_conn.fetchval(
        "SELECT id FROM milestones WHERE id = $1",
        ms_id,
    )
    assert still_exists is None, f"Milestone {ms_id} still in DB after DELETE 204"


# ---------------------------------------------------------------------------
# GET /api/milestones/upcoming — cross-project deadline feed (owner-scoped)
# ---------------------------------------------------------------------------


async def _seed_ms(
    conn, project_id: int, user_id: int, name: str, offset_days: int, completed=False
):
    return await conn.fetchval(
        """INSERT INTO milestones (project_id, name, deadline, completed, user_id)
           VALUES ($1, $2, NOW() + make_interval(days => $3), $4, $5) RETURNING id""",
        project_id,
        name,
        offset_days,
        completed,
        user_id,
    )


async def test_upcoming_milestones_window_and_scoping(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """?within_days=7 returns only A's incomplete, future, in-window milestones
    ordered by deadline with project_name — excludes out-of-window, completed,
    past-due, and user-B rows."""
    pid_a = contract_two_users.project_id_a
    ms_2d = await _seed_ms(contract_conn, pid_a, contract_two_users.user_a_id, "MS +2d", 2)
    ms_5d = await _seed_ms(contract_conn, pid_a, contract_two_users.user_a_id, "MS +5d", 5)
    ms_20d = await _seed_ms(contract_conn, pid_a, contract_two_users.user_a_id, "MS +20d", 20)
    ms_done = await _seed_ms(
        contract_conn, pid_a, contract_two_users.user_a_id, "MS done", 3, completed=True
    )
    ms_past = await _seed_ms(contract_conn, pid_a, contract_two_users.user_a_id, "MS past", -2)
    # User B milestone within the window — must not appear for A.
    ms_b = await _seed_ms(
        contract_conn, contract_two_users.project_id_b, contract_two_users.user_b_id, "B +2d", 2
    )

    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/milestones/upcoming?within_days=7")

    assert resp.status_code == 200, (
        f"GET /api/milestones/upcoming failed: {resp.status_code}: {resp.text[:300]}"
    )
    rows = resp.json()
    returned_ids = [m["id"] for m in rows]
    assert returned_ids == [ms_2d, ms_5d], (
        f"expected only A's +2d,+5d ordered by deadline; got {returned_ids}"
    )
    assert ms_20d not in returned_ids, "out-of-window milestone leaked"
    assert ms_done not in returned_ids, "completed milestone leaked"
    assert ms_past not in returned_ids, "past-due milestone leaked"
    assert ms_b not in returned_ids, "scoping leak: user B's milestone returned to A"
    assert all(m["project_name"] is not None for m in rows), "project_name missing from JOIN"


async def test_upcoming_milestones_bounds_validation(
    contract_two_users, _le_app, _configure_api_key
):
    """within_days bounds: 0 → 422, 91 → 422."""
    async with _client(_le_app, contract_two_users.cookie_a) as c:
        r_low = await c.get("/api/milestones/upcoming?within_days=0")
        r_high = await c.get("/api/milestones/upcoming?within_days=91")

    assert r_low.status_code == 422, f"within_days=0 expected 422, got {r_low.status_code}"
    assert r_high.status_code == 422, f"within_days=91 expected 422, got {r_high.status_code}"
