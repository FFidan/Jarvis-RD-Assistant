"""Tasks CRUD contract tests — A221, A222, A223, A224, A225, A226.

Covers:
- GET /api/projects/{id}/tasks           (A221) — caller's tasks; 404 for non-owner project
- POST /api/projects/{id}/tasks          (A222) — row inserted with user_id; 404 for non-owner
- PUT /api/tasks/{id}                    (A223) — owner update; 404 for non-owner
- DELETE /api/tasks/{id}                 (A224) — owner delete; 404 for non-owner
- POST /api/tasks/{id}/papers            (A225) — link row inserted; 404 for non-owner task
- DELETE /api/tasks/{id}/papers/{paper}  (A226) — link deleted for owner; 404 for non-owner
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
# §A221 — GET /api/projects/{id}/tasks — scoping + non-owner 404
# ---------------------------------------------------------------------------


async def test_list_tasks_non_owner_project_gets_404(
    contract_two_users, _le_app, _configure_api_key
):
    """User B cannot list tasks for user A's project — 404 (IDOR guard).

    Exercises the real ``SELECT id FROM projects WHERE id = $1 AND user_id = $2``
    ownership check in list_tasks.
    """
    project_id = contract_two_users.project_id_a
    async with _client(_le_app, contract_two_users.cookie_b) as c:
        resp = await c.get(f"/api/projects/{project_id}/tasks")

    assert resp.status_code == 404, (
        f"IDOR: user B got {resp.status_code} listing tasks for user A's project "
        f"{project_id} (expected 404). Body: {resp.text[:300]}"
    )


async def test_list_tasks_owner_sees_own_task(contract_two_users, _le_app, _configure_api_key):
    """User A can list tasks for their own project and sees their seeded task.

    Positive control: confirms the project_id + user_id filter returns the
    seeded task_id_a row.
    """
    project_id = contract_two_users.project_id_a
    task_id_a = contract_two_users.task_id_a
    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp = await c.get(f"/api/projects/{project_id}/tasks")

    assert resp.status_code == 200, (
        f"GET /api/projects/{project_id}/tasks for owner failed: "
        f"{resp.status_code}: {resp.text[:300]}"
    )
    task_ids = [t["id"] for t in resp.json()]
    assert task_id_a in task_ids, f"User A expected to see their own task {task_id_a} in {task_ids}"


# ---------------------------------------------------------------------------
# §A222 — POST /api/projects/{id}/tasks — row inserted with user_id; 404 non-owner
# ---------------------------------------------------------------------------


async def test_create_task_row_has_caller_user_id(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """POST /api/projects/{id}/tasks creates a task with the caller's user_id in DB."""
    project_id = contract_two_users.project_id_a
    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp = await c.post(
            f"/api/projects/{project_id}/tasks",
            json={"title": "Contract Created Task"},
        )

    assert resp.status_code == 201, f"POST tasks failed: {resp.status_code}: {resp.text[:300]}"
    task_id = resp.json()["id"]
    db_user_id = await contract_conn.fetchval(
        "SELECT user_id FROM tasks WHERE id = $1",
        task_id,
    )
    assert db_user_id == contract_two_users.user_a_id, (
        f"Task {task_id} has user_id={db_user_id}; expected {contract_two_users.user_a_id}"
    )


async def test_create_task_non_owner_project_gets_404(
    contract_two_users, _le_app, _configure_api_key
):
    """User B cannot create a task in user A's project — 404."""
    project_id = contract_two_users.project_id_a
    async with _client(_le_app, contract_two_users.cookie_b) as c:
        resp = await c.post(
            f"/api/projects/{project_id}/tasks",
            json={"title": "Injected Task"},
        )

    assert resp.status_code == 404, (
        f"IDOR: user B got {resp.status_code} creating task in user A's project "
        f"{project_id} (expected 404). Body: {resp.text[:300]}"
    )


# ---------------------------------------------------------------------------
# §A223 — PUT /api/tasks/{id} — owner update; 404 for non-owner
# ---------------------------------------------------------------------------


async def test_update_task_owner_gets_200(contract_two_users, _le_app, _configure_api_key):
    """User A can update their own task — 200 with updated fields."""
    task_id = contract_two_users.task_id_a
    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp = await c.put(f"/api/tasks/{task_id}", json={"title": "Updated Task Title"})

    assert resp.status_code == 200, (
        f"Owner expected 200 updating task {task_id}; got {resp.status_code}: {resp.text[:300]}"
    )
    assert resp.json()["title"] == "Updated Task Title"


async def test_update_task_user_b_gets_404(contract_two_users, _le_app, _configure_api_key):
    """User B cannot update user A's task — 404 (IDOR guard)."""
    task_id = contract_two_users.task_id_a
    async with _client(_le_app, contract_two_users.cookie_b) as c:
        resp = await c.put(f"/api/tasks/{task_id}", json={"title": "Hijacked Title"})

    assert resp.status_code != 401, f"PUT /api/tasks/{task_id}: got 401 — session wiring bug"
    assert resp.status_code == 404, (
        f"IDOR: user B got {resp.status_code} updating user A's task {task_id} "
        f"(expected 404). Body: {resp.text[:300]}"
    )


# ---------------------------------------------------------------------------
# §A224 — DELETE /api/tasks/{id} — owner delete; 404 for non-owner
# ---------------------------------------------------------------------------


async def test_delete_task_user_b_gets_404(contract_two_users, _le_app, _configure_api_key):
    """User B cannot delete user A's task — 404 (IDOR guard)."""
    task_id = contract_two_users.task_id_a
    async with _client(_le_app, contract_two_users.cookie_b) as c:
        resp = await c.delete(f"/api/tasks/{task_id}")

    assert resp.status_code != 401, f"DELETE /api/tasks/{task_id}: got 401 — session wiring bug"
    assert resp.status_code == 404, (
        f"IDOR: user B got {resp.status_code} deleting user A's task {task_id} "
        f"(expected 404). Body: {resp.text[:300]}"
    )


async def test_delete_task_owner_gets_204(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """User A can delete their own task — 204 and row gone from DB."""
    project_id = contract_two_users.project_id_a
    new_task_id = await contract_conn.fetchval(
        "INSERT INTO tasks (project_id, title, user_id) VALUES ($1, 'Deletable Task', $2) "
        "RETURNING id",
        project_id,
        contract_two_users.user_a_id,
    )
    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp = await c.delete(f"/api/tasks/{new_task_id}")

    assert resp.status_code == 204, (
        f"Owner expected 204 deleting task {new_task_id}; got {resp.status_code}: {resp.text[:300]}"
    )
    still_exists = await contract_conn.fetchval(
        "SELECT id FROM tasks WHERE id = $1",
        new_task_id,
    )
    assert still_exists is None, f"Task {new_task_id} still in DB after DELETE 204"


# ---------------------------------------------------------------------------
# §A225 — POST /api/tasks/{id}/papers — link row inserted; 404 non-owner task
# ---------------------------------------------------------------------------


async def test_link_paper_non_owner_task_gets_404(contract_two_users, _le_app, _configure_api_key):
    """User B cannot link a paper to user A's task — 404 (IDOR guard)."""
    task_id_a = contract_two_users.task_id_a
    paper_id_a = contract_two_users.paper_id_a
    async with _client(_le_app, contract_two_users.cookie_b) as c:
        resp = await c.post(
            f"/api/tasks/{task_id_a}/papers",
            json={"paper_id": paper_id_a},
        )

    assert resp.status_code == 404, (
        f"IDOR: user B got {resp.status_code} linking paper to user A's task {task_id_a} "
        f"(expected 404). Body: {resp.text[:300]}"
    )


async def test_link_paper_to_task_creates_row(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """POST /api/tasks/{id}/papers creates a task_paper_links row for owner.

    Also verifies that a duplicate link returns 409 (unique constraint).
    """
    task_id_a = contract_two_users.task_id_a
    paper_id_a = contract_two_users.paper_id_a

    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp = await c.post(
            f"/api/tasks/{task_id_a}/papers",
            json={"paper_id": paper_id_a},
        )

    assert resp.status_code == 201, (
        f"POST /api/tasks/{task_id_a}/papers failed: {resp.status_code}: {resp.text[:300]}"
    )
    row = await contract_conn.fetchrow(
        "SELECT * FROM task_paper_links WHERE task_id = $1 AND paper_id = $2",
        task_id_a,
        paper_id_a,
    )
    assert row is not None, (
        f"task_paper_links row not found for task={task_id_a} paper={paper_id_a}"
    )

    # Duplicate link must return 409
    async with _client(_le_app, contract_two_users.cookie_a) as c:
        dup_resp = await c.post(
            f"/api/tasks/{task_id_a}/papers",
            json={"paper_id": paper_id_a},
        )
    assert dup_resp.status_code == 409, (
        f"Duplicate link expected 409; got {dup_resp.status_code}: {dup_resp.text[:300]}"
    )


# ---------------------------------------------------------------------------
# §A226 — DELETE /api/tasks/{id}/papers/{paper} — owner unlinks; 404 non-owner
# ---------------------------------------------------------------------------


async def test_unlink_paper_non_owner_task_gets_404(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """User B cannot unlink a paper from user A's task — 404 (IDOR guard)."""
    task_id_a = contract_two_users.task_id_a
    paper_id_a = contract_two_users.paper_id_a
    # Ensure the link exists first
    await contract_conn.execute(
        "INSERT INTO task_paper_links (task_id, paper_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
        task_id_a,
        paper_id_a,
    )

    async with _client(_le_app, contract_two_users.cookie_b) as c:
        resp = await c.delete(f"/api/tasks/{task_id_a}/papers/{paper_id_a}")

    assert resp.status_code == 404, (
        f"IDOR: user B got {resp.status_code} unlinking paper from user A's task "
        f"{task_id_a} (expected 404). Body: {resp.text[:300]}"
    )
