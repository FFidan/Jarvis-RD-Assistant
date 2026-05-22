"""Shared IDOR contract suite (audit X-07).

Asserts user B cannot read or mutate user A's owned resources via the
authorized API surface. Parametrized over (method, path, owned_attr, kind)
quadruples; each quadruple proves either: owner gets 200/204 or non-owner
gets 403/404 (depending on the route's contract).

This is STRICTLY STRONGER than the per-domain IDOR sprawl scattered across
services: it exercises the real assert_paper_ownership / user_id-scoped SQL
paths against a real DB via the contract layer, not a monkeypatch.setattr
of the resolver.

The existing test_cross_user_isolation.py integration test stays — it
exercises the full SessionMiddleware → cookie → resolver path; this contract
suite is at the verify_api_key + ownership-helper layer (different rung).
"""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from jarvis_common.testing_contract_apps import (
    DEFAULT_CONTRACT_API_KEY,
)
from jarvis_common.testing_contract_apps import (
    make_contract_client as _make_client,
)

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]


# (method, path_template, owned_attr, kind)
#
# Grounding evidence (each triple verified against route at HEAD):
#
# paper read/mutate — notes.py:56, papers.py:254, papers_service.py:229, priority.py:44
# notes read/mutate — notes.py:56, notes.py:105, notes.py:155-169, notes.py:327-343
# citations — citations.py:46, citations.py:81, citations.py:96
#
# "byid"   → non-owner expected {403,404}; owner expected {200}
# "mutate" → non-owner expected {403,404}; owner expected {200,204} (or 409 on state guard)
#
# Excluded routes:
#   GET /api/papers/{paper_id} — asyncpg prepared-stmt cache issue: the owner
#     control path executes a query with `$1::text` cast and SharedConnPool
#     passes an int, triggering DataError 500. The IDOR claim (user B → 403)
#     is exercised by the integration test (full pool, no SharedConnPool).
#   POST /api/citations/{paper_id}/fetch — S2 source unavailable in the contract
#     environment (no network/S2 dependency injected); returns 503 for both
#     owner A and attacker B (ownership check fires first for B → 403 ✓ via
#     integration test; owner A hits 503 before producing a useful control).
#   POST /api/papers/{paper_id}/feedback — ownership check fires first (403 for B),
#     but owner A gets 400 because seeded paper has discovery_origin='user_initiated';
#     can't assert 200 for A without custom seeding outside _seed_resources.
#   DELETE /api/papers/{paper_id}/feedback — no ownership check; WHERE user_id=$2 is
#     the natural scope; idempotent 204 for all callers (no IDOR risk, nothing to assert).
#   POST /api/notes/{note_id}/promote — requires Zotero annotation source='zotero';
#     seeded note has source='user', returns 400 before reaching ownership assert
#     for owner; non-owner still correctly gets 404 but can't test owner control path.
#   /api/ask*, /api/search*, /api/similar — require ollama+qdrant (out of scope,
#     per vault open-question 2026-05-18).
#
IDOR_QUADRUPLES: list[tuple[str, str, str, str]] = [
    # --- paper state mutations ---
    # papers.py:524 — await papers_service.assert_paper_ownership(conn, paper_id, user_id)
    ("PUT", "/api/papers/{paper_id_a}/save", "paper_id_a", "mutate"),
    # papers.py:550 — await papers_service.assert_paper_ownership(conn, paper_id, user_id)
    ("PUT", "/api/papers/{paper_id_a}/unsave", "paper_id_a", "mutate"),
    # papers.py:571 — await papers_service.assert_paper_ownership(conn, paper_id, user_id)
    # skip; A's paper state is to_read (seeded), not inbox — skip returns 409 for owner
    # ("PUT", "/api/papers/{paper_id_a}/skip", "paper_id_a", "mutate"),
    # papers.py:592 — await papers_service.assert_paper_ownership(conn, paper_id, user_id)
    ("PUT", "/api/papers/{paper_id_a}/reading", "paper_id_a", "mutate"),
    # papers.py:615 — await papers_service.assert_paper_ownership(conn, paper_id, user_id)
    ("PUT", "/api/papers/{paper_id_a}/done", "paper_id_a", "mutate"),
    # papers.py:653 — await papers_service.assert_paper_ownership(conn, paper_id, user_id)
    ("PUT", "/api/papers/{paper_id_a}/star", "paper_id_a", "mutate"),
    # papers.py:709 — await papers_service.assert_paper_ownership(conn, paper_id, user_id)
    ("PUT", "/api/papers/{paper_id_a}/unstar", "paper_id_a", "mutate"),
    # papers.py:729 — await papers_service.assert_paper_ownership(conn, paper_id, user_id)
    ("PUT", "/api/papers/{paper_id_a}/trash", "paper_id_a", "mutate"),
    # papers.py:807 — await papers_service.assert_paper_ownership(conn, paper_id, user_id)
    # annotations requires a valid partial body; rating/user_notes/flagged all optional
    ("PUT", "/api/papers/{paper_id_a}/annotations", "paper_id_a", "mutate"),
    # --- notes ---
    # notes.py:56 — await assert_paper_ownership(conn, paper_id, user_id)
    ("GET", "/api/papers/{paper_id_a}/notes", "paper_id_a", "byid"),
    # notes.py:105 — await assert_paper_ownership(conn, paper_id, user_id)
    ("POST", "/api/papers/{paper_id_a}/notes", "paper_id_a", "mutate"),
    # notes.py:155-165 — WHERE id=$1 AND user_id=$2 → 404 for non-owner
    ("PUT", "/api/notes/{note_id_a}", "note_id_a", "mutate"),
    # notes.py:327-339 — WHERE id=$1 AND user_id=$2 → 404 for non-owner
    ("DELETE", "/api/notes/{note_id_a}", "note_id_a", "mutate"),
    # --- priority ---
    # priority.py:44 — await assert_paper_ownership(conn, paper_id, user_id)
    ("POST", "/api/papers/{paper_id_a}/priority", "paper_id_a", "mutate"),
    # --- citations ---
    # citations.py:96 — await assert_paper_ownership(conn, paper_id, user_id)
    ("GET", "/api/citations/{paper_id_a}", "paper_id_a", "byid"),
]

# ---------------------------------------------------------------------------
# LE IDOR quadruples: projects / tasks / milestones  (A266, A274 — UNCOVERED)
#
# Grounding evidence (routes verified against HEAD):
#
# projects.py:128 — WHERE id = $1 AND user_id = $2 (GET /api/projects/{id})
# projects.py:195 — WHERE id = $1 AND user_id = $2 FOR UPDATE (PUT /api/projects/{id})
# projects.py:234 — DELETE FROM projects WHERE id = $1 AND user_id = $2 (DELETE)
# tasks.py:57 — SELECT id FROM projects WHERE id = $1 AND user_id = $2 (GET tasks list)
# tasks.py:104 — SELECT id FROM projects WHERE id = $1 AND user_id = $2 (POST task)
# tasks.py:157 — WHERE id = $1 AND user_id = $2 FOR UPDATE (PUT /api/tasks/{id})
# tasks.py:203 — DELETE FROM tasks WHERE id = $1 AND user_id = $2 (DELETE)
# milestones.py:31 — SELECT id FROM projects WHERE id = $1 AND user_id = $2 (GET list)
# milestones.py:70 — SELECT id FROM projects WHERE id = $1 AND user_id = $2 (POST)
# milestones.py:110 — WHERE id = $1 AND user_id = $2 FOR UPDATE (PUT /api/milestones/{id})
# milestones.py:156 — DELETE FROM milestones WHERE id = $1 AND user_id = $2 (DELETE)
#
# NOTE: All LE routes use user_id-scoped SQL rather than assert_paper_ownership.
# The ownership predicate is: WHERE id = $1 AND user_id = $2 (or project ownership
# for nested routes). A non-owner gets 404 (no matching row) for all of them.
#
# Excluded routes:
#   GET /api/projects/{id}/tasks — nested under project; project owner check fires
#     first and returns 404; no task_id_a in contract quadruple (path uses project_id).
#     Covered by test_le_contract.py::test_get_project_detail_user_b_gets_404.
#   GET /api/projects/{id}/milestones — same pattern; covered via project ownership.
#   POST /api/projects/{id}/tasks — project check gates it (no task creation possible).
#   PUT /api/tasks/{task_id_a}/papers — requires paper_id in body + task AND paper
#     ownership check; complex seed outside TwoUsers. Covered in test_le_contract.
# ---------------------------------------------------------------------------

LE_IDOR_QUADRUPLES: list[tuple[str, str, str, str]] = [
    # projects.py:128 — WHERE id = $1 AND user_id = $2
    ("GET", "/api/projects/{project_id_a}", "project_id_a", "byid"),
    # projects.py:195 — WHERE id = $1 AND user_id = $2 FOR UPDATE
    ("PUT", "/api/projects/{project_id_a}", "project_id_a", "mutate"),
    # projects.py:234 — DELETE FROM projects WHERE id = $1 AND user_id = $2
    ("DELETE", "/api/projects/{project_id_a}", "project_id_a", "mutate"),
    # tasks.py:157 — WHERE id = $1 AND user_id = $2 FOR UPDATE
    ("PUT", "/api/tasks/{task_id_a}", "task_id_a", "mutate"),
    # tasks.py:203 — DELETE FROM tasks WHERE id = $1 AND user_id = $2
    ("DELETE", "/api/tasks/{task_id_a}", "task_id_a", "mutate"),
]

# Minimal valid JSON bodies for LE write verbs — must reach the ownership guard
# without triggering 422 schema rejection.
_LE_BODIES: dict[str, dict] = {
    "/api/projects/{project_id_a}": {"name": "hijacked-project"},
    "/api/tasks/{task_id_a}": {"title": "hijacked-task"},
}


def _le_ids() -> list[str]:
    return [f"{m}:{p}:{k}" for m, p, _, k in LE_IDOR_QUADRUPLES]


# ---------------------------------------------------------------------------
# Minimal valid-ish JSON bodies for write verbs (schema-shaped so the request
# reaches the auth/ownership guard, not a 422 short-circuit).
_BODIES: dict[str, dict] = {
    "/api/papers/{paper_id_a}/annotations": {"user_notes": "x"},
    "/api/papers/{paper_id_a}/notes": {"user_note": "intrusion-attempt"},
    "/api/notes/{note_id_a}": {"user_note": "tampered"},
}


def _resolve(template: str, tu) -> str:
    return template.format(
        paper_id_a=tu.paper_id_a,
        note_id_a=tu.note_id_a,
    )


def _le_resolve(template: str, tu) -> str:
    return template.format(
        project_id_a=tu.project_id_a,
        task_id_a=tu.task_id_a,
    )


def _ids() -> list[str]:
    return [f"{m}:{p}:{k}" for m, p, _, k in IDOR_QUADRUPLES]


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def _pi_app_with_pool(contract_conn):
    """paper_ingestion app with its state.db_pool wired to the contract conn."""
    from jarvis_common.testing import SharedConnPool
    from paper_ingestion.main import app

    shared = SharedConnPool(contract_conn)
    original = getattr(app.state, "db_pool", None)
    app.state.db_pool = shared
    yield app
    if original is None:
        if hasattr(app.state, "db_pool"):
            del app.state.db_pool
    else:
        app.state.db_pool = original


@pytest.mark.parametrize("method,path_template,owned_attr,kind", IDOR_QUADRUPLES, ids=_ids())
async def test_user_b_cannot_access_user_a_resource(
    method,
    path_template,
    owned_attr,
    kind,
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
):
    """User B (attacker) gets 403 or 404 on user A's owned resource."""
    path = _resolve(path_template, contract_two_users)
    body = _BODIES.get(path_template)

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_b) as c:
        resp = await c.request(method, path, json=body)

    assert resp.status_code != 401, (
        f"{method} {path}: got 401 — session cookie failed to authenticate user B "
        f"(fixture/middleware wiring bug, not isolation)"
    )
    assert resp.status_code in {403, 404}, (
        f"{method} {path}: user B got {resp.status_code} for user A's {owned_attr} "
        f"(expected 403/404). Body: {resp.text[:300]}"
    )


@pytest.mark.parametrize("method,path_template,owned_attr,kind", IDOR_QUADRUPLES, ids=_ids())
async def test_user_a_can_access_own_resource(
    method,
    path_template,
    owned_attr,
    kind,
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
):
    """User A (owner) gets 200/204 (or 409 on guarded state transitions) on their own resource."""
    path = _resolve(path_template, contract_two_users)
    body = _BODIES.get(path_template)

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.request(method, path, json=body)

    assert resp.status_code != 401, (
        f"{method} {path}: got 401 — session cookie failed to authenticate user A "
        f"(fixture/middleware wiring bug)"
    )
    # 201 = Created (POST /api/papers/{paper_id}/notes).
    # 409 = state-guard conflict (e.g. unsave on to_read paper succeeds with 200, but
    # some verbs like skip require inbox state — seeded state is to_read, so the
    # ownership check passes and only the state guard fires a 409). This is the
    # correct ownership-allowed path: the resource was found and belonged to A.
    assert resp.status_code in {200, 201, 204, 409}, (
        f"{method} {path}: user A got {resp.status_code} on their own {owned_attr}. "
        f"Body: {resp.text[:300]}"
    )


# ---------------------------------------------------------------------------
# LE IDOR — projects / tasks / milestones (A266, A274)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def _le_app_with_pool(contract_conn):
    """learning_engine app with db_pool + mocked non-DB state wired to the contract conn."""
    from datetime import UTC, datetime, timedelta
    from unittest.mock import AsyncMock, MagicMock

    from jarvis_common.testing import SharedConnPool
    from learning_engine.deps import get_anki_exporter, get_db_pool, get_fsrs_manager, limiter
    from learning_engine.main import app

    shared = SharedConnPool(contract_conn)

    mock_fsrs = MagicMock()
    _now = datetime.now(UTC)
    mock_fsrs.create_new_card.return_value = ({}, _now)
    mock_fsrs.schedule_review.return_value = ({}, {}, _now + timedelta(days=1))

    originals = {
        k: getattr(app.state, k, None)
        for k in ("db_pool", "http_client", "fsrs_manager", "anki_exporter", "card_generator")
    }
    app.state.db_pool = shared
    app.state.http_client = AsyncMock()
    app.state.fsrs_manager = mock_fsrs
    app.state.anki_exporter = MagicMock()
    app.state.card_generator = AsyncMock()
    app.dependency_overrides[get_db_pool] = lambda: shared
    app.dependency_overrides[get_fsrs_manager] = lambda: mock_fsrs
    app.dependency_overrides[get_anki_exporter] = lambda: MagicMock()

    limiter_was_enabled = limiter.enabled
    limiter.enabled = False

    try:
        yield app
    finally:
        limiter.enabled = limiter_was_enabled
        for attr, val in originals.items():
            if val is None:
                if hasattr(app.state, attr):
                    delattr(app.state, attr)
            else:
                setattr(app.state, attr, val)
        app.dependency_overrides.pop(get_db_pool, None)
        app.dependency_overrides.pop(get_fsrs_manager, None)
        app.dependency_overrides.pop(get_anki_exporter, None)


def _make_le_client(app, cookie: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"X-API-Key": DEFAULT_CONTRACT_API_KEY},
        cookies={"jarvis_session": cookie},
    )


@pytest.mark.parametrize("method,path_template,owned_attr,kind", LE_IDOR_QUADRUPLES, ids=_le_ids())
async def test_le_user_b_cannot_access_user_a_resource(
    method,
    path_template,
    owned_attr,
    kind,
    contract_two_users,
    _le_app_with_pool,
    _configure_api_key,
):
    """User B (attacker) gets 404 on user A's LE-owned resource.

    LE ownership is enforced via ``WHERE id = $1 AND user_id = $2``; a
    non-matching row yields 404 (not 403) for all LE routes.

    Covers predicate rows A266 + A274 from the coverage map.
    """
    path = _le_resolve(path_template, contract_two_users)
    body = _LE_BODIES.get(path_template)

    async with _make_le_client(_le_app_with_pool, contract_two_users.cookie_b) as c:
        resp = await c.request(method, path, json=body)

    assert resp.status_code != 401, (
        f"{method} {path}: got 401 — session cookie failed to authenticate user B "
        f"(fixture/middleware wiring bug, not isolation)"
    )
    assert resp.status_code in {403, 404}, (
        f"{method} {path}: user B got {resp.status_code} for user A's {owned_attr} "
        f"(expected 403/404). Body: {resp.text[:300]}"
    )


@pytest.mark.parametrize("method,path_template,owned_attr,kind", LE_IDOR_QUADRUPLES, ids=_le_ids())
async def test_le_user_a_can_access_own_resource(
    method,
    path_template,
    owned_attr,
    kind,
    contract_two_users,
    _le_app_with_pool,
    _configure_api_key,
):
    """User A (owner) gets 200/204 on their own LE resource.

    Positive control: confirms the user_id-scoped SQL accepts the owning user
    (not just rejects the non-owner).
    """
    path = _le_resolve(path_template, contract_two_users)
    body = _LE_BODIES.get(path_template)

    async with _make_le_client(_le_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.request(method, path, json=body)

    assert resp.status_code != 401, (
        f"{method} {path}: got 401 — session cookie failed to authenticate user A "
        f"(fixture/middleware wiring bug)"
    )
    # DELETE /api/projects/{id} cascades: 204. PUT variants: 200. GET: 200.
    assert resp.status_code in {200, 201, 204}, (
        f"{method} {path}: user A got {resp.status_code} on their own {owned_attr}. "
        f"Body: {resp.text[:300]}"
    )
