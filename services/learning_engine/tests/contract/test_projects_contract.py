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


async def test_get_project_cross_tenant_returns_404(
    contract_two_users, _le_app, _configure_api_key
):
    """User B GET on user A's project detail → 404 (scoping holds under owner-override resolver).

    GET /api/projects/{id} now resolves identity via the owner-override
    resolver, but the ``WHERE id = $1 AND user_id = $2`` filter still scopes
    the session-authenticated caller (user B) to their own rows.
    """
    project_id = contract_two_users.project_id_a
    async with _client(_le_app, contract_two_users.cookie_b) as c:
        resp = await c.get(f"/api/projects/{project_id}")

    assert resp.status_code != 401, f"GET /api/projects/{project_id}: got 401 — session wiring bug"
    assert resp.status_code == 404, (
        f"cross-tenant: user B got {resp.status_code} reading user A's project {project_id} "
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


# ---------------------------------------------------------------------------
# §A217 — project_papers CRUD — user-scoping + IDOR guards
# ---------------------------------------------------------------------------


async def test_project_papers_list_user_scoped(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """GET /api/projects/{id}/papers returns user A's linked paper; user B gets 404.

    Verifies the ``WHERE id = $1 AND user_id = $2`` IDOR guard in
    list_project_papers — accessing user A's project as user B must 404.
    """
    project_id = contract_two_users.project_id_a
    paper_id = contract_two_users.paper_id_a

    # Seed a project_papers link directly — avoids Zotero/library side-effects.
    await contract_conn.execute(
        "INSERT INTO project_papers (project_id, paper_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
        project_id,
        paper_id,
    )

    # User A sees their paper in the project list.
    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp_a = await c.get(f"/api/projects/{project_id}/papers")
    assert resp_a.status_code == 200, (
        f"User A GET /api/projects/{project_id}/papers failed: {resp_a.status_code}: {resp_a.text[:200]}"
    )
    ids_a = [p["id"] for p in resp_a.json()]
    assert paper_id in ids_a, (
        f"User A's paper {paper_id} not found in project {project_id} list: {ids_a}"
    )

    # User B gets 404 on user A's project — IDOR guard.
    async with _client(_le_app, contract_two_users.cookie_b) as c:
        resp_b = await c.get(f"/api/projects/{project_id}/papers")
    assert resp_b.status_code != 401, (
        f"GET /api/projects/{project_id}/papers as user B: got 401 — session wiring bug"
    )
    assert resp_b.status_code == 404, (
        f"IDOR: user B got {resp_b.status_code} on user A's project {project_id} "
        f"(expected 404). Body: {resp_b.text[:200]}"
    )


async def test_project_papers_attach_validates_paper_ownership(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """POST /api/projects/{id}/papers/{paper_id} rejects cross-user paper attach.

    User A tries to link user B's paper (discovered_by=user_b_id, not in A's
    library) into user A's project.  assert_paper_ownership must raise 403.
    """
    project_id = contract_two_users.project_id_a

    # Fetch user B's paper id — only *_a resources are on the fixture object.
    paper_id_b = await contract_conn.fetchval(
        "SELECT id FROM papers WHERE discovered_by = $1",
        contract_two_users.user_b_id,
    )
    assert paper_id_b is not None, "Seed error: user B has no paper in DB"

    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp = await c.post(f"/api/projects/{project_id}/papers/{paper_id_b}")

    assert resp.status_code in (403, 404), (
        f"Expected 403 or 404 when attaching user B's paper {paper_id_b} to user A's project "
        f"{project_id}; got {resp.status_code}: {resp.text[:200]}"
    )
    # The link must NOT exist in DB.
    linked = await contract_conn.fetchval(
        "SELECT 1 FROM project_papers WHERE project_id = $1 AND paper_id = $2",
        project_id,
        paper_id_b,
    )
    assert linked is None, (
        f"IDOR: cross-user paper {paper_id_b} was inserted into project {project_id} despite rejection"
    )


async def test_project_papers_attach_idempotent_envelope(
    contract_two_users, contract_conn, _le_app, _configure_api_key, _research_library_command
):
    """POST /api/projects/{id}/papers/{paper_id} twice → identical
    {project_id, paper_id} envelope on both calls.

    Pins the envelope-consistency contract: the already-linked
    early-return path returns the same two-key shape as the freshly-linked
    path and no longer carries a ``message`` field. Exercises the link_paper
    success path, which queries ``papers.zotero_item_key`` (schema
    recovery added the missing column to the bedrock).
    """
    project_id = contract_two_users.project_id_a
    paper_id = contract_two_users.paper_id_a

    async with _client(_le_app, contract_two_users.cookie_a) as c:
        first = await c.post(f"/api/projects/{project_id}/papers/{paper_id}")
        second = await c.post(f"/api/projects/{project_id}/papers/{paper_id}")

    assert first.status_code == 201, (
        f"first link expected 201, got {first.status_code}: {first.text[:200]}"
    )
    assert second.status_code == 201, (
        f"already-linked expected 201, got {second.status_code}: {second.text[:200]}"
    )
    assert first.json() == {"project_id": project_id, "paper_id": paper_id}
    assert second.json() == {"project_id": project_id, "paper_id": paper_id}


async def test_project_papers_detach_idor_rejected(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """DELETE /api/projects/{id}/papers/{paper_id} by user B on user A's link → 404.

    Verifies the ``USING projects p … p.user_id = $3`` ownership guard in
    unlink_paper prevents user B from removing a paper from user A's project.
    """
    project_id = contract_two_users.project_id_a
    paper_id = contract_two_users.paper_id_a

    # Seed the link so there is something to attempt to delete.
    await contract_conn.execute(
        "INSERT INTO project_papers (project_id, paper_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
        project_id,
        paper_id,
    )

    async with _client(_le_app, contract_two_users.cookie_b) as c:
        resp = await c.delete(f"/api/projects/{project_id}/papers/{paper_id}")

    assert resp.status_code != 401, (
        f"DELETE /api/projects/{project_id}/papers/{paper_id} as user B: got 401 — session wiring bug"
    )
    assert resp.status_code == 404, (
        f"IDOR: user B got {resp.status_code} deleting user A's project link "
        f"(expected 404). Body: {resp.text[:200]}"
    )
    # Link must still exist in DB.
    still_linked = await contract_conn.fetchval(
        "SELECT 1 FROM project_papers WHERE project_id = $1 AND paper_id = $2",
        project_id,
        paper_id,
    )
    assert still_linked is not None, (
        f"IDOR: user B successfully deleted project_papers link "
        f"(project={project_id}, paper={paper_id}) — ownership guard failed"
    )


# ---------------------------------------------------------------------------
# M8b — get_project counts are user-scoped (defense-in-depth)
# ---------------------------------------------------------------------------


async def test_get_project_detail_question_count_excludes_other_users_rows(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """open_question_count counts only the caller's project_questions rows.

    Defense-in-depth for the LATERAL count in get_project: a project_questions
    row carrying another user's user_id on the same project_id must NOT be
    counted for the owner.
    """
    # Verified: services/learning_engine/learning_engine/routers/projects.py:162-165
    # Fresh project → deterministic zero baseline for all counts.
    async with _client(_le_app, contract_two_users.cookie_a) as c:
        create_resp = await c.post("/api/projects", json={"name": "M8b Count Scoping"})
    assert create_resp.status_code == 201, (
        f"POST /api/projects failed: {create_resp.status_code}: {create_resp.text[:300]}"
    )
    project_id = create_resp.json()["id"]

    # One question owned by user A, one cross-user row stamped with user B's id.
    await contract_conn.execute(
        "INSERT INTO project_questions (project_id, user_id, body) VALUES ($1, $2, $3)",
        project_id,
        contract_two_users.user_a_id,
        "own open question",
    )
    await contract_conn.execute(
        "INSERT INTO project_questions (project_id, user_id, body) VALUES ($1, $2, $3)",
        project_id,
        contract_two_users.user_b_id,
        "cross-user question that must not be counted",
    )

    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp = await c.get(f"/api/projects/{project_id}")
    assert resp.status_code == 200, (
        f"GET /api/projects/{project_id} failed: {resp.status_code}: {resp.text[:300]}"
    )
    body = resp.json()
    assert body["open_question_count"] == 1, (
        f"open_question_count must exclude other users' rows: expected 1, "
        f"got {body['open_question_count']}"
    )
    assert body["paper_count"] == 0, (
        f"Fresh project must have paper_count 0; got {body['paper_count']}"
    )


# ---------------------------------------------------------------------------
# M8c — link_paper: zotero.push enqueue unavailability is loud, not fatal
# ---------------------------------------------------------------------------


async def test_project_papers_attach_zotero_enqueue_unavailable_is_loud_not_fatal(
    contract_two_users,
    contract_conn,
    _le_app,
    _configure_api_key,
    _research_library_command,
    monkeypatch,
    caplog,
):
    """Linking a starred paper still returns 201 when zotero.push is not in the
    task registry, and the skip is logged at ERROR level (not silently swallowed).

    Pins the M8c fix: the registry lookup lives outside the enqueue try/except,
    so a missing registration surfaces as a distinct ERROR log while the link
    itself (already committed) succeeds.
    """
    # Verified: services/learning_engine/learning_engine/routers/project_papers.py:104-155
    import logging as _logging
    from types import MappingProxyType

    from jarvis_common import task_registry as _task_registry

    project_id = contract_two_users.project_id_a
    paper_id = contract_two_users.paper_id_a

    # Star the paper so link_paper takes the zotero-push branch.
    await contract_conn.execute(
        "INSERT INTO paper_user_state (paper_id, user_id, starred) VALUES ($1, $2, TRUE) "
        "ON CONFLICT (paper_id, user_id) DO UPDATE SET starred = TRUE",
        paper_id,
        contract_two_users.user_a_id,
    )
    # Force the missing-registration path deterministically — other test modules
    # in the same process may have populated the process-global registry.
    monkeypatch.setattr(_task_registry, "KIND_TO_TASK", MappingProxyType({}))

    with caplog.at_level(_logging.ERROR, logger="learning_engine.routers.project_papers"):
        async with _client(_le_app, contract_two_users.cookie_a) as c:
            resp = await c.post(f"/api/projects/{project_id}/papers/{paper_id}")

    assert resp.status_code == 201, (
        f"link_paper must succeed despite unavailable zotero.push enqueue; "
        f"got {resp.status_code}: {resp.text[:300]}"
    )
    assert resp.json() == {"project_id": project_id, "paper_id": paper_id}
    error_messages = [r.getMessage() for r in caplog.records if r.levelno >= _logging.ERROR]
    assert any("zotero.push" in m for m in error_messages), (
        f"Missing zotero.push registration must be logged at ERROR; got {error_messages}"
    )
