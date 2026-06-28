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


async def test_create_task_parent_owned_by_other_user_gets_404(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """User A cannot parent a new task under user B's task — 404, no row written (IDOR).

    The project FK only proves the parent exists; without an ownership check the
    caller could attach a child under another tenant's task and leak structure.
    """
    project_id_a = contract_two_users.project_id_a
    b_task_id = await contract_conn.fetchval(
        "INSERT INTO tasks (project_id, title, user_id) VALUES ($1, 'B private task', $2) "
        "RETURNING id",
        contract_two_users.project_id_b,
        contract_two_users.user_b_id,
    )
    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp = await c.post(
            f"/api/projects/{project_id_a}/tasks",
            json={"title": "IDOR child", "parent_task_id": b_task_id},
        )

    assert resp.status_code == 404, (
        f"IDOR: user A got {resp.status_code} parenting under user B's task {b_task_id} "
        f"(expected 404). Body: {resp.text[:300]}"
    )
    count = await contract_conn.fetchval("SELECT count(*) FROM tasks WHERE title = 'IDOR child'")
    assert count == 0, f"IDOR child task was written despite 404 ({count} rows)"


async def test_create_task_parent_owned_by_caller_gets_201(
    contract_two_users, _le_app, _configure_api_key
):
    """Positive control: user A can parent a new task under their own task — 201."""
    project_id_a = contract_two_users.project_id_a
    parent_task_id = contract_two_users.task_id_a
    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp = await c.post(
            f"/api/projects/{project_id_a}/tasks",
            json={"title": "Owned subtask", "parent_task_id": parent_task_id},
        )

    assert resp.status_code == 201, (
        f"Owner expected 201 parenting under own task {parent_task_id}; "
        f"got {resp.status_code}: {resp.text[:300]}"
    )


async def test_create_task_cross_project_parent_same_user_gets_404(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """User A cannot parent a task in project A2 under their own task in project A1 — 404.

    The parent guard must check project_id, not only user_id.  Without the
    project_id predicate the cross-project parent lookup succeeds and the child
    is created with a dangling cross-project FK, violating the project boundary.
    """
    project_id_a2 = await contract_conn.fetchval(
        "INSERT INTO projects (name, user_id) VALUES ('A-second-project-parent-guard', $1) RETURNING id",
        contract_two_users.user_a_id,
    )
    task_in_a2 = await contract_conn.fetchval(
        "INSERT INTO tasks (project_id, title, user_id) VALUES ($1, 'A task in project 2', $2) RETURNING id",
        project_id_a2,
        contract_two_users.user_a_id,
    )
    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp = await c.post(
            f"/api/projects/{contract_two_users.project_id_a}/tasks",
            json={"title": "Cross-project child", "parent_task_id": task_in_a2},
        )

    assert resp.status_code == 404, (
        f"Cross-project parent boundary violated: user A got {resp.status_code} "
        f"parenting under task {task_in_a2} in different project (expected 404). "
        f"Body: {resp.text[:300]}"
    )
    count = await contract_conn.fetchval(
        "SELECT count(*) FROM tasks WHERE title = 'Cross-project child'"
    )
    assert count == 0, f"Cross-project child task was written despite 404 ({count} rows)"


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


async def test_update_task_cross_tenant_returns_404(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """User B's PUT against user A's task → 404, and A's row is unchanged.

    Even though update_task now resolves identity via the owner-override
    resolver, the ``WHERE id = $1 AND user_id = $2`` ownership filter still
    scopes the session-authenticated caller (user B) to their own rows.
    """
    task_id = contract_two_users.task_id_a
    before = await contract_conn.fetchval("SELECT title FROM tasks WHERE id = $1", task_id)

    async with _client(_le_app, contract_two_users.cookie_b) as c:
        resp = await c.put(f"/api/tasks/{task_id}", json={"title": "ZZZ-CROSS-TENANT-HIJACK"})

    assert resp.status_code == 404, (
        f"cross-tenant: user B got {resp.status_code} updating user A's task {task_id} "
        f"(expected 404). Body: {resp.text[:300]}"
    )
    after = await contract_conn.fetchval("SELECT title FROM tasks WHERE id = $1", task_id)
    assert after == before, (
        f"cross-tenant write leaked: task {task_id} title changed from {before!r} to {after!r}"
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


# ---------------------------------------------------------------------------
# BUG-1 (S8) — PUT /api/tasks/{id} status→done bumps daily_log.tasks_completed
# update_task is the sole writer of daily_log.tasks_completed.
# ---------------------------------------------------------------------------


async def _mark_done(le_app, cookie: str, task_id: int):
    async with _client(le_app, cookie) as c:
        return await c.put(f"/api/tasks/{task_id}", json={"status": "done"})


async def test_status_to_done_increments_daily_log(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """First todo→done transition today inserts daily_log with tasks_completed=1."""
    task_id = contract_two_users.task_id_a
    resp = await _mark_done(_le_app, contract_two_users.cookie_a, task_id)
    assert resp.status_code == 200, f"mark-done failed: {resp.status_code}: {resp.text[:300]}"

    count = await contract_conn.fetchval(
        "SELECT tasks_completed FROM daily_log WHERE user_id = $1 AND log_date = CURRENT_DATE",
        contract_two_users.user_a_id,
    )
    assert count == 1, f"expected tasks_completed=1 after one done transition, got {count!r}"


async def test_second_done_same_day_increments_to_two(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """A second distinct task→done the same day bumps the counter to 2 (ON CONFLICT path)."""
    task_id_1 = contract_two_users.task_id_a
    task_id_2 = await contract_conn.fetchval(
        "INSERT INTO tasks (project_id, title, status, user_id) "
        "VALUES ($1, 'Second task', 'todo', $2) RETURNING id",
        contract_two_users.project_id_a,
        contract_two_users.user_a_id,
    )

    r1 = await _mark_done(_le_app, contract_two_users.cookie_a, task_id_1)
    r2 = await _mark_done(_le_app, contract_two_users.cookie_a, task_id_2)
    assert r1.status_code == 200 and r2.status_code == 200

    count = await contract_conn.fetchval(
        "SELECT tasks_completed FROM daily_log WHERE user_id = $1 AND log_date = CURRENT_DATE",
        contract_two_users.user_a_id,
    )
    assert count == 2, f"expected tasks_completed=2 after two done transitions, got {count!r}"


async def test_redone_already_done_task_does_not_increment(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """Re-PUT status=done on an already-done task must NOT double-count (idempotent guard)."""
    task_id = contract_two_users.task_id_a
    first = await _mark_done(_le_app, contract_two_users.cookie_a, task_id)
    assert first.status_code == 200
    # Second PUT: existing status is already 'done' → guard suppresses the bump.
    second = await _mark_done(_le_app, contract_two_users.cookie_a, task_id)
    assert second.status_code == 200

    count = await contract_conn.fetchval(
        "SELECT tasks_completed FROM daily_log WHERE user_id = $1 AND log_date = CURRENT_DATE",
        contract_two_users.user_a_id,
    )
    assert count == 1, f"re-PUT of already-done task double-counted: expected 1, got {count!r}"


async def test_done_with_null_tasks_completed_coalesces_to_one(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """An existing daily_log row with NULL tasks_completed becomes 1, not NULL (COALESCE path)."""
    # Seed today's row with an explicit NULL counter (column is nullable).
    await contract_conn.execute(
        "INSERT INTO daily_log (user_id, log_date, tasks_completed) "
        "VALUES ($1, CURRENT_DATE, NULL)",
        contract_two_users.user_a_id,
    )
    task_id = contract_two_users.task_id_a
    resp = await _mark_done(_le_app, contract_two_users.cookie_a, task_id)
    assert resp.status_code == 200

    count = await contract_conn.fetchval(
        "SELECT tasks_completed FROM daily_log WHERE user_id = $1 AND log_date = CURRENT_DATE",
        contract_two_users.user_a_id,
    )
    assert count == 1, f"COALESCE path failed: expected 1 from NULL+1, got {count!r}"


# ---------------------------------------------------------------------------
# GET /api/tasks — cross-project task list (owner-scoped, LEFT JOIN project_name)
# ---------------------------------------------------------------------------


async def test_list_all_tasks_in_progress_across_projects_scoped_to_caller(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """?status=in_progress returns only A's in-progress tasks across all A's
    projects, each carrying its project_name; never leaks user B's tasks."""
    # A second project for user A, plus one in_progress task in each project.
    project_id_a2 = await contract_conn.fetchval(
        "INSERT INTO projects (name, user_id) VALUES ('A-second-project', $1) RETURNING id",
        contract_two_users.user_a_id,
    )
    t_a1 = await contract_conn.fetchval(
        "INSERT INTO tasks (project_id, title, status, user_id) "
        "VALUES ($1, 'A inprog 1', 'in_progress', $2) RETURNING id",
        contract_two_users.project_id_a,
        contract_two_users.user_a_id,
    )
    t_a2 = await contract_conn.fetchval(
        "INSERT INTO tasks (project_id, title, status, user_id) "
        "VALUES ($1, 'A inprog 2', 'in_progress', $2) RETURNING id",
        project_id_a2,
        contract_two_users.user_a_id,
    )
    # User B in_progress task — must not appear.
    await contract_conn.execute(
        "INSERT INTO tasks (project_id, title, status, user_id) "
        "VALUES ($1, 'B inprog', 'in_progress', $2)",
        contract_two_users.project_id_b,
        contract_two_users.user_b_id,
    )

    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/tasks?status=in_progress")

    assert resp.status_code == 200, f"GET /api/tasks failed: {resp.status_code}: {resp.text[:300]}"
    rows = resp.json()
    returned_ids = {t["id"] for t in rows}
    assert {t_a1, t_a2} <= returned_ids, f"missing A's in-progress tasks: {returned_ids}"
    assert all(t["status"] == "in_progress" for t in rows)
    # Each carries a non-null project_name (both belong to a project).
    by_id = {t["id"]: t for t in rows}
    assert by_id[t_a1]["project_name"] is not None
    assert by_id[t_a2]["project_name"] == "A-second-project"


async def test_list_all_tasks_project_less_task_has_null_project_name(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """A quick-add task with project_id NULL appears via LEFT JOIN with project_name None."""
    t_null = await contract_conn.fetchval(
        "INSERT INTO tasks (project_id, title, status, user_id) "
        "VALUES (NULL, 'Quick add', 'in_progress', $1) RETURNING id",
        contract_two_users.user_a_id,
    )
    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/tasks?status=in_progress")

    assert resp.status_code == 200
    by_id = {t["id"]: t for t in resp.json()}
    assert t_null in by_id, "LEFT JOIN dropped the project-less task"
    assert by_id[t_null]["project_id"] is None
    assert by_id[t_null]["project_name"] is None


async def test_list_all_tasks_other_users_project_id_returns_empty(
    contract_two_users, _le_app, _configure_api_key
):
    """?project_id pointing at user B's project returns [] for user A (scoped by user_id, no leak)."""
    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp = await c.get(f"/api/tasks?project_id={contract_two_users.project_id_b}")

    assert resp.status_code == 200
    assert resp.json() == [], "scoping leak: user A saw rows when filtering on user B's project_id"


async def test_list_all_tasks_bounds_validation(contract_two_users, _le_app, _configure_api_key):
    """limit/status bounds: limit=0 → 422, limit=201 → 422, status=bogus → 422."""
    async with _client(_le_app, contract_two_users.cookie_a) as c:
        r_low = await c.get("/api/tasks?limit=0")
        r_high = await c.get("/api/tasks?limit=201")
        r_status = await c.get("/api/tasks?status=bogus")

    assert r_low.status_code == 422, f"limit=0 expected 422, got {r_low.status_code}"
    assert r_high.status_code == 422, f"limit=201 expected 422, got {r_high.status_code}"
    assert r_status.status_code == 422, f"status=bogus expected 422, got {r_status.status_code}"


async def test_list_all_tasks_empty_returns_empty_list(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """A filter matching no rows returns [] (not an error)."""
    # No 'blocked' tasks seeded for user A.
    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/tasks?status=blocked")
    assert resp.status_code == 200
    assert resp.json() == []
