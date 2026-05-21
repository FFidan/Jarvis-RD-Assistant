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

pytestmark = [pytest.mark.contract, pytest.mark.asyncio(loop_scope="session")]

_TEST_API_KEY = "idor-contract-shared-key-do-not-use-in-prod"

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


def _ids() -> list[str]:
    return [f"{m}:{p}:{k}" for m, p, _, k in IDOR_QUADRUPLES]


@pytest.fixture(scope="function")
def _configure_api_key(monkeypatch):
    from jarvis_common import auth as _auth
    from jarvis_common.settings import get_secrets_settings

    monkeypatch.setenv("JARVIS_API_KEY", _TEST_API_KEY)
    get_secrets_settings.cache_clear()
    _auth.refresh_api_key_cache()
    yield
    get_secrets_settings.cache_clear()
    _auth.refresh_api_key_cache()


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


def _make_client(app, cookie: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"X-API-Key": _TEST_API_KEY},
        cookies={"jarvis_session": cookie},
    )


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
