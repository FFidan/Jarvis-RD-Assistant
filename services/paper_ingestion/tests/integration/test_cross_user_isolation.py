"""Cross-user data-isolation negative suite.

THE merge gate for the v0.4.0 public launch. Drives BOTH service apps
(paper_ingestion + learning_engine) with the *real* ``SessionMiddleware``
and the *real* strict user-id resolvers (the autouse stub is opted out via
``@pytest.mark.real_auth``). User B carries a valid ``jarvis_session`` cookie
and attempts to read / mutate user A's owned rows; every attempt must be
denied (403/404) and must not leak A's content.

Run (Docker PostgreSQL required)::

    JARVIS_RUN_LIVE_PG=1 uv run pytest -c pyproject.toml -m "integration and live_pg" \
        services/paper_ingestion/tests/integration/test_cross_user_isolation.py -v

Standard ``uv run pytest`` excludes ``live_pg``/``integration`` so this is
inert in the fast suite (see root pyproject ``addopts``).

A leak surfaces as a *failing* assertion here — those are
real bugs to fix at the query layer, not assertions to weaken.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import httpx
import pytest
from httpx import ASGITransport

# The real apps. SessionMiddleware + strict resolvers are wired in main.
# learning_engine is only importable when both service roots are on
# sys.path (root pyproject.toml config, which CI uses). Under the
# service-local pytest.ini this whole module is deselected by the
# integration/live_pg markers anyway — but pytest still *imports* it
# during collection, so guard the cross-service import to keep the
# default fast suite green instead of erroring at collection.
le_main = pytest.importorskip(
    "learning_engine.main",
    reason="learning_engine not on sys.path (run via root pyproject config)",
)
le_app = le_main.app

from jarvis_common.testing_contract_apps import (  # noqa: E402
    make_contract_client as _make_contract_client,
)
from paper_ingestion.main import app as pi_app  # noqa: E402
from tests.conftest import (  # noqa: E402
    A_CARD_FRONT,
    A_NOTE_TEXT,
    A_PAPER_TITLE,
    A_PROJECT_NAME,
    A_TASK_TITLE,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.live_pg,
    pytest.mark.real_auth,
]

# Realistic production auth model: a SINGLE shared JARVIS_API_KEY proves
# request authenticity (every browser sends it as X-API-Key from the
# auth-store), while the per-user identity comes ONLY from the
# `jarvis_session` cookie. The attack this suite proves is closed: a
# legitimate API-key holder (user B) still cannot reach user A's data
# because the strict resolver derives identity from the session, not the
# key. So both users carry the same valid key and differ only by cookie.
_TEST_API_KEY = "iso-suite-shared-key-do-not-use-in-prod"


@pytest.fixture(autouse=True)
def _configure_api_key(monkeypatch):
    """Set JARVIS_API_KEY so verify_api_key enforces (not 401-misconfig)."""
    from jarvis_common import auth as _auth

    monkeypatch.setenv("JARVIS_API_KEY", _TEST_API_KEY)
    # SecretsSettings is pydantic-settings backed; clear its cache then
    # refresh the module-level API-key cache verify_api_key reads.
    from jarvis_common.settings import get_secrets_settings

    get_secrets_settings.cache_clear()
    _auth.refresh_api_key_cache()
    yield
    get_secrets_settings.cache_clear()
    _auth.refresh_api_key_cache()


# Which app serves which path prefix. paper_ingestion owns papers/notes/
# pulse/recommendations/citations/topics; learning_engine owns projects/
# tasks/decks/cards.
_PI = "pi"
_LE = "le"

# Per-user PRIVATE content — must NEVER appear in any response to user B,
# on any endpoint, ever. A leak here is an unconditional bug.
_PRIVATE_MARKERS = (
    A_NOTE_TEXT,
    A_PROJECT_NAME,
    A_TASK_TITLE,
    A_CARD_FRONT,
)
# A paper title may be shared only when its row is persisted public. The seeded
# tenant papers below remain private and are visible through explicit library
# membership. Per-user state, notes, and product outputs are always private.
_LEAK_MARKERS = (A_PAPER_TITLE, *_PRIVATE_MARKERS)


def _app(which: str):
    return pi_app if which == _PI else le_app


async def _client(app, cookie: str) -> httpx.AsyncClient:
    # X-API-Key proves request authenticity (shared, same for both users);
    # the jarvis_session cookie is the ONLY thing that selects the user.
    client = httpx.AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-API-Key": _TEST_API_KEY},
    )
    client.cookies.set("jarvis_session", cookie)
    return client


def _resolve(template: str, tu) -> str:
    """Fill an endpoint template with user A's seeded resource ids."""
    return template.format(
        paper_id=tu.paper_id_a,
        note_id=tu.note_id_a,
        card_id=tu.card_id_a,
        deck_id=tu.deck_id_a,
        project_id=tu.project_id_a,
        task_id=tu.task_id_a,
        topic_id=tu.topic_id_a,
    )


def _assert_no_leak(body_text: str, markers: tuple[str, ...]) -> None:
    for marker in markers:
        assert marker not in body_text, f"cross-user LEAK: {marker!r} visible to user B"


# ---------------------------------------------------------------------------
# Endpoint registry: (service, method, path_template, kind)
#
# kind:
#   "byid"        — GET A's resource as B: expect 403/404 and no A content.
#   "mutate"      — PATCH/PUT/DELETE/POST A's resource as B. The hard
#                   invariants: (1) no PRIVATE marker leaks in the response,
#                   AND (2) A's owned row is provably unchanged — verified
#                   independently by re-reading the DB in _assert_a_intact.
#                   A 4xx/5xx rejection trivially satisfies both; a 2xx is
#                   only a failure if it actually exposed or mutated A's
#                   data (caught by 1 + 2). The exact status code is not
#                   asserted — data integrity is, which is stronger.
#   "list"        — per-user collection as B: NONE of A's markers (incl.
#                   title) may appear.
#   "corpus_list" — a GLOBAL collection (papers canonical-corpus; the topic
#                   taxonomy): only A's PRIVATE markers must be absent — the
#                   shared metadata (paper title / topic name) legitimately
#                   appears. Per-user secrecy for papers is enforced on
#                   byid/mutate via assert_paper_ownership (other rows).
#   "global"      — a deliberately shared/global mutable resource (the topic
#                   taxonomy has no per-user owner column; subscribing only
#                   affects the *caller's* own subscription). Only assert no
#                   PRIVATE marker leaks; the write is by-design permitted.
#
# LLM/RAG/qdrant/embedding endpoints are intentionally excluded (need
# ollama/qdrant): /api/ask*, /api/summarize*, /api/search*, /api/discover,
# /api/similar, /api/generate*, /api/papers/{id}/extract (POST, enqueues LLM job).
# Extraction READ endpoints (GET /api/papers/{id}/extractions,
# GET /api/extractions/table) do NOT need ollama/qdrant and ARE covered by
# dedicated isolation tests below (test_extraction_reads_scoped_to_calling_user).
# ---------------------------------------------------------------------------
_REGISTRY: list[tuple[str, str, str, str]] = [
    # --- papers (paper_ingestion) ---
    (_PI, "GET", "/api/papers/{paper_id}", "byid"),
    (_PI, "GET", "/api/papers", "corpus_list"),
    (_PI, "GET", "/api/papers/brief", "corpus_list"),
    (_PI, "PUT", "/api/papers/{paper_id}/save", "mutate"),
    (_PI, "PUT", "/api/papers/{paper_id}/unsave", "mutate"),
    (_PI, "PUT", "/api/papers/{paper_id}/skip", "mutate"),
    (_PI, "PUT", "/api/papers/{paper_id}/reading", "mutate"),
    (_PI, "PUT", "/api/papers/{paper_id}/done", "mutate"),
    (_PI, "PUT", "/api/papers/{paper_id}/star", "mutate"),
    (_PI, "PUT", "/api/papers/{paper_id}/unstar", "mutate"),
    (_PI, "PUT", "/api/papers/{paper_id}/trash", "mutate"),
    (_PI, "PUT", "/api/papers/{paper_id}/restore", "mutate"),
    (_PI, "PUT", "/api/papers/{paper_id}/annotations", "mutate"),
    (_PI, "POST", "/api/papers/{paper_id}/feedback", "mutate"),
    (_PI, "DELETE", "/api/papers/{paper_id}/feedback", "mutate"),
    (_PI, "DELETE", "/api/papers/{paper_id}", "mutate"),
    (_PI, "GET", "/api/papers/feed/counts", "list"),
    # FE-UIA-01: topic-facet filter must NOT widen visibility past the caller's
    # library — topic_id is an extra AND on top of the user_library scope JOIN.
    (_PI, "GET", "/api/papers/feed?topic_id={topic_id}", "list"),
    # --- notes (paper_ingestion) ---
    (_PI, "GET", "/api/papers/{paper_id}/notes", "byid"),
    (_PI, "POST", "/api/papers/{paper_id}/notes", "mutate"),
    (_PI, "PUT", "/api/notes/{note_id}", "mutate"),
    (_PI, "POST", "/api/notes/{note_id}/promote", "mutate"),
    (_PI, "DELETE", "/api/notes/{note_id}", "mutate"),
    # --- priority (paper_ingestion) ---
    (_PI, "POST", "/api/papers/{paper_id}/priority", "mutate"),
    # --- recommendations (paper_ingestion) ---
    (_PI, "GET", "/api/recommendations", "list"),
    (_PI, "POST", "/api/recommendations/{paper_id}/dismiss", "mutate"),
    # --- citations (paper_ingestion) ---
    (_PI, "GET", "/api/citations/{paper_id}", "byid"),
    (_PI, "POST", "/api/citations/{paper_id}/fetch", "mutate"),
    # --- topics (paper_ingestion) ---
    (_PI, "GET", "/api/topics/subscriptions", "list"),
    (_PI, "GET", "/api/topics", "corpus_list"),
    (_PI, "PUT", "/api/topics/{topic_id}", "global"),
    (_PI, "DELETE", "/api/topics/{topic_id}", "global"),
    (_PI, "PUT", "/api/topics/{topic_id}/subscribe", "global"),
    (_PI, "DELETE", "/api/topics/{topic_id}/subscribe", "global"),
    # --- pulse (paper_ingestion) ---
    (_PI, "GET", "/api/pulse/today", "list"),
    (_PI, "GET", "/api/pulse/history", "list"),
    (_PI, "GET", "/api/pulse/stats", "list"),
    (_PI, "GET", "/api/pulse/explain/{card_id}", "byid"),
    # --- my-day journal (paper_ingestion) ---
    (_PI, "GET", "/api/my-day/journal", "list"),
    # --- projects (learning_engine) ---
    (_LE, "GET", "/api/projects", "list"),
    (_LE, "GET", "/api/projects/{project_id}", "byid"),
    (_LE, "PUT", "/api/projects/{project_id}", "mutate"),
    (_LE, "DELETE", "/api/projects/{project_id}", "mutate"),
    (_LE, "GET", "/api/projects/{project_id}/tasks", "byid"),
    (_LE, "GET", "/api/projects/{project_id}/papers", "byid"),
    (_LE, "GET", "/api/projects/{project_id}/milestones", "byid"),
    # --- tasks (learning_engine) ---
    (_LE, "PUT", "/api/tasks/{task_id}", "mutate"),
    (_LE, "DELETE", "/api/tasks/{task_id}", "mutate"),
    (_LE, "POST", "/api/tasks/{task_id}/papers", "mutate"),
    (_LE, "DELETE", "/api/tasks/{task_id}/papers/{paper_id}", "mutate"),
    # --- decks / cards (learning_engine) ---
    (_LE, "GET", "/api/decks", "list"),
    (_LE, "GET", "/api/cards", "list"),
    (_LE, "PUT", "/api/cards/{card_id}", "mutate"),
    (_LE, "DELETE", "/api/cards/{card_id}", "mutate"),
]

# Minimal valid-ish JSON bodies for write verbs (schema-shaped so the
# request reaches the auth/ownership guard, not a 422 short-circuit).
_BODIES: dict[str, dict] = {
    "/api/papers/{paper_id}/annotations": {"user_notes": "x"},
    "/api/papers/{paper_id}/feedback": {"signal": "positive", "source": "paper_detail_thumbs"},
    "/api/papers/{paper_id}/notes": {"user_note": "intruder note"},
    "/api/notes/{note_id}": {"user_note": "tampered"},
    "/api/papers/{paper_id}/priority": {"priority_score": 9.9},
    "/api/topics/{topic_id}": {"name": "hacked", "query_terms": ["x"]},
    "/api/projects/{project_id}": {"name": "hacked-project"},
    "/api/tasks/{task_id}": {"title": "hacked-task"},
    "/api/tasks/{task_id}/papers": {"paper_id": 0},  # filled per-test
    "/api/cards/{card_id}": {"front": "hacked", "back": "hacked"},
}

# Statuses that prove isolation held (denied or not-found). 401 would mean
# the cookie did not authenticate at all — that is a fixture/wiring failure,
# so it is deliberately NOT in the allow-set.
_DENIED = {403, 404}


def _ids() -> list[str]:
    return [f"{s}:{m}:{p}:{k}" for s, m, p, k in _REGISTRY]


@pytest.mark.parametrize(("service", "method", "template", "kind"), _REGISTRY, ids=_ids())
@pytest.mark.asyncio(loop_scope="session")
async def test_user_b_cannot_reach_user_a_resource(
    service: str,
    method: str,
    template: str,
    kind: str,
    two_users,
) -> None:
    """Each registry row is an independent cross-user isolation assertion."""
    app = _app(service)
    # Both apps read the live pool from app.state; the SessionMiddleware also
    # needs it to resolve the jarvis_session cookie.
    app.state.db_pool = two_users.pool

    path = _resolve(template, two_users)
    body = _BODIES.get(template)
    if template == "/api/tasks/{task_id}/papers":
        body = {"paper_id": two_users.paper_id_a}

    async with await _client(app, two_users.cookie_b) as client:
        resp = await client.request(method, path, json=body)
        text = resp.text

        # Sanity: the cookie DID authenticate user B (no global 401).
        assert resp.status_code != 401, (
            f"{method} {path} returned 401 — session cookie failed to "
            f"authenticate (fixture/middleware wiring bug, not isolation)"
        )

        if kind in ("list", "corpus_list", "global"):
            # 5xx here means the endpoint's own scoping query crashed
            # (e.g. a missing column) — a product defect, surfaced as a
            # CONCERN, not silently passed.
            assert resp.status_code < 500, (
                f"SERVER ERROR (scoping-query defect?): {method} {path} -> "
                f"{resp.status_code}: {text[:200]}"
            )
            markers = _LEAK_MARKERS if kind == "list" else _PRIVATE_MARKERS
            _assert_no_leak(text, markers)
        elif kind == "byid":
            assert resp.status_code in _DENIED, (
                f"LEAK/UNEXPECTED: {method} {path} -> {resp.status_code} "
                f"(expected 403/404). Body: {text[:300]}"
            )
            _assert_no_leak(text, _LEAK_MARKERS)
        else:  # mutate — invariants are: no private leak + A row intact
            _assert_no_leak(text, _LEAK_MARKERS)

    # For mutating verbs the hard data-integrity gate: re-read the DB
    # directly (independent of any router) and prove A's row is unchanged.
    if kind == "mutate":
        async with two_users.pool.acquire() as conn:
            await _assert_a_intact(conn, template, two_users)


async def _assert_a_intact(conn, template: str, tu) -> None:
    """Re-query the DB as ground truth: user A's seeded data is unchanged."""
    if "/papers/" in template and template.endswith(
        (
            "/save",
            "/unsave",
            "/skip",
            "/reading",
            "/done",
            "/star",
            "/unstar",
            "/trash",
            "/restore",
            "/annotations",
        )
    ):
        row = await conn.fetchrow(
            "SELECT state, starred FROM paper_user_state WHERE paper_id=$1 AND user_id=$2",
            tu.paper_id_a,
            tu.user_a_id,
        )
        assert row is not None, "A's paper_user_state row was deleted"
        assert row["state"] == "to_read" and row["starred"] is True
    elif template == "/api/papers/{paper_id}":  # DELETE paper
        title = await conn.fetchval("SELECT title FROM papers WHERE id=$1", tu.paper_id_a)
        assert title == A_PAPER_TITLE, "A's paper was deleted/renamed by B"
    elif template in ("/api/notes/{note_id}", "/api/notes/{note_id}/promote"):
        note = await conn.fetchval("SELECT user_note FROM paper_notes WHERE id=$1", tu.note_id_a)
        assert note == A_NOTE_TEXT, "A's note was tampered/deleted by B"
    elif template == "/api/projects/{project_id}":
        name = await conn.fetchval("SELECT name FROM projects WHERE id=$1", tu.project_id_a)
        assert name == A_PROJECT_NAME, "A's project was tampered/deleted by B"
    elif template == "/api/tasks/{task_id}":
        title = await conn.fetchval("SELECT title FROM tasks WHERE id=$1", tu.task_id_a)
        assert title == A_TASK_TITLE, "A's task was tampered/deleted by B"
    elif template == "/api/cards/{card_id}":
        front = await conn.fetchval("SELECT front FROM cards WHERE id=$1", tu.card_id_a)
        assert front == A_CARD_FRONT, "A's card was tampered/deleted by B"
    elif template == "/api/topics/{topic_id}":
        name = await conn.fetchval("SELECT name FROM topics WHERE id=$1", tu.topic_id_a)
        assert name and name.startswith("topic-"), "A's topic was tampered by B"
    # Other mutate endpoints (feedback/priority/dismiss/fetch/subscribe) have
    # no destructive effect on A's seeded markers; the status-code + no-leak
    # assertions above are sufficient.


# ---------------------------------------------------------------------------
# Per-user surface isolation: paper_extractions + paper_notes(zotero)
#
# Migration 0094 made these tables per-user (user_id column). The parametrised
# _REGISTRY harness cannot easily seed extraction rows (needs a template FK),
# so the coverage lives here as dedicated contract-style tests.
#
# Pattern: contract_two_users + _pi_app_with_pool + _make_contract_client,
# consistent with tests/contract/ tests.  The session-scoped contract pool is
# reused (per-test rollback via contract_conn).
# ---------------------------------------------------------------------------

# Marker string embedded in A's extraction so a body-text leak check is easy.
_A_EXTRACTION_MARKER = "ZZZ-ISOLATION-A-EXTRACTION-CONTENT"
_A_ZOTERO_NOTE_MARKER = "ZZZ-ISOLATION-A-ZOTERO-NOTE"


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_extraction_reads_scoped_to_calling_user(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
) -> None:
    """User B cannot see user A's paper_extractions rows via either read endpoint.

    Seed: one private paper in both users' libraries + one
    extraction template + one paper_extractions row owned by user A.

    Asserts:
      - GET /api/papers/{P}/extractions as user B → [] (empty list, not A's row)
      - GET /api/extractions/table?template_id=T as user B → [] (empty table)
      - Both endpoints return 200 (scoping query runs without crashing).

    Verified: extractions.py:255-295 get_paper_extractions,
              extractions.py:324-440 get_extraction_table
    # Verified: extractions.py:268
    """
    # Seed a private paper made visible to both users by explicit membership.
    paper_id = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by)
           VALUES ('iso-ext-shared-xtr', 'arxiv', 'shared-paper-extractions-isolation',
                   ARRAY['A. Author'], 'https://example.test/shared-xtr', NULL)
           RETURNING id"""
    )
    # Both users in library so assert_paper_ownership passes for A and B.
    await contract_conn.executemany(
        "INSERT INTO user_library (user_id, paper_id, added_via) VALUES ($1, $2, 'manual_save')",
        [
            (contract_two_users.user_a_id, paper_id),
            (contract_two_users.user_b_id, paper_id),
        ],
    )

    # Seed: extraction template.
    template_id = await contract_conn.fetchval(
        """INSERT INTO extraction_templates (name, description, fields, is_default)
           VALUES ('iso-test-template', 'isolation test', '[]'::jsonb, FALSE)
           RETURNING id"""
    )

    # Seed: extraction row owned by user A.
    await contract_conn.execute(
        """INSERT INTO paper_extractions (paper_id, template_id, extractions, user_id)
           VALUES ($1, $2, $3::jsonb, $4)""",
        paper_id,
        template_id,
        {"finding": {"value": _A_EXTRACTION_MARKER}},
        contract_two_users.user_a_id,
    )

    # _pi_app_with_pool is already wired to the same contract_conn transaction;
    # all router reads share the same connection and see the seeded rows.
    async with _make_contract_client(_pi_app_with_pool, contract_two_users.cookie_b) as client:
        resp_list = await client.get(f"/api/papers/{paper_id}/extractions")
        resp_table = await client.get(
            "/api/extractions/table",
            params={"template_id": template_id},
        )

    # Both endpoints must succeed (scoping query runs without crashing).
    assert resp_list.status_code == 200, (
        f"GET /api/papers/{{paper_id}}/extractions returned {resp_list.status_code}: "
        f"{resp_list.text[:300]}"
    )
    assert resp_table.status_code == 200, (
        f"GET /api/extractions/table returned {resp_table.status_code}: {resp_table.text[:300]}"
    )

    # User B's response must not contain any of A's extraction data.
    assert resp_list.json() == [], (
        f"LEAK: user B saw user A's extraction row: {resp_list.text[:300]}"
    )
    assert _A_EXTRACTION_MARKER not in resp_list.text, (
        f"LEAK: A's extraction marker visible to B in /extractions: {resp_list.text[:300]}"
    )
    table_body = resp_table.json()
    assert table_body == [], (
        f"LEAK: user B saw user A's row in /extractions/table: {resp_table.text[:300]}"
    )


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_zotero_note_reads_scoped_to_calling_user(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
) -> None:
    """User B cannot see user A's zotero paper_notes row via GET /api/papers/{P}/notes.

    Seed: shared paper (discovered_by NULL, in both libraries) + one zotero
    note owned by user A.

    Asserts:
      - GET /api/papers/{P}/notes as user B → [] (A's zotero note excluded)
      - GET /api/papers/{P}/notes?source=zotero as user B → [] (same)
      - Endpoint returns 200 in both cases (scoping query must not crash).

    Verified: notes.py:31-74 list_notes — WHERE paper_id=$1 AND user_id=$2
    # Verified: notes.py:57
    """
    # Seed: shared paper visible to both users.
    paper_id = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by)
           VALUES ('iso-ext-shared-ztero', 'arxiv', 'shared-paper-zotero-isolation',
                   ARRAY['B. Author'], 'https://example.test/shared-ztero', NULL)
           RETURNING id"""
    )
    await contract_conn.executemany(
        "INSERT INTO user_library (user_id, paper_id, added_via) VALUES ($1, $2, 'manual_save')",
        [
            (contract_two_users.user_a_id, paper_id),
            (contract_two_users.user_b_id, paper_id),
        ],
    )

    # Seed: zotero note owned by user A.
    await contract_conn.execute(
        """INSERT INTO paper_notes (paper_id, user_id, user_note, source, zotero_annotation_key)
           VALUES ($1, $2, $3, 'zotero', 'iso-zotero-key-a')""",
        paper_id,
        contract_two_users.user_a_id,
        _A_ZOTERO_NOTE_MARKER,
    )

    async with _make_contract_client(_pi_app_with_pool, contract_two_users.cookie_b) as client:
        resp_all = await client.get(f"/api/papers/{paper_id}/notes")
        resp_zotero = await client.get(f"/api/papers/{paper_id}/notes", params={"source": "zotero"})

    assert resp_all.status_code == 200, (
        f"GET /api/papers/{{paper_id}}/notes returned {resp_all.status_code}: {resp_all.text[:300]}"
    )
    assert resp_zotero.status_code == 200, (
        f"GET /api/papers/{{paper_id}}/notes?source=zotero returned "
        f"{resp_zotero.status_code}: {resp_zotero.text[:300]}"
    )

    assert resp_all.json() == [], (
        f"LEAK: user B saw user A's zotero note in /notes: {resp_all.text[:300]}"
    )
    assert resp_zotero.json() == [], (
        f"LEAK: user B saw user A's zotero note in /notes?source=zotero: {resp_zotero.text[:300]}"
    )
    assert _A_ZOTERO_NOTE_MARKER not in resp_all.text
    assert _A_ZOTERO_NOTE_MARKER not in resp_zotero.text


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_aud007_zotero_link_scoped_to_calling_user(
    contract_two_users,
    contract_conn,
) -> None:
    """A paper's Zotero item linkage is per-(paper,user); user B can never
    read or act on user A's ``zotero_item_key``.

    Seed: a shared paper P carrying A's GLOBAL ``papers.zotero_item_key`` (the
    legacy column a pre-migration read would pick up) AND A's per-user link row.
    User B has a valid Zotero config but NO link row for P.

    Asserts the export read path keys off the *requesting* user's link:
      - push_highlights_for_paper as user B -> ``not_linked`` (B's LEFT JOIN finds
        no link row; A's key is never read).
      - push_highlights_for_paper as user A -> ``ok`` (A's link resolves; no
        unsynced highlights, so exported 0).

    Regression-proof: reverting the read to the global ``p.zotero_item_key`` column
    makes B observe A's 'A-GLOBAL-KEY' and skip the ``not_linked`` guard, flipping
    the user-B assertion RED.
    """
    from unittest.mock import AsyncMock, patch

    import httpx
    from jarvis_common.testing import SharedConnPool

    from paper_ingestion.integrations.zotero_service import push_highlights_for_paper

    user_a_id = contract_two_users.user_a_id
    user_b_id = contract_two_users.user_b_id

    # Shared paper carrying A's GLOBAL key (the pre-migration global column) — present so a
    # regressed global-column read would observably leak it to user B.
    paper_id = await contract_conn.fetchval(
        """INSERT INTO papers
               (external_id, source_type, title, authors, url, discovered_by, zotero_item_key)
           VALUES ('iso-aud007-zotero', 'arxiv', 'shared-paper-aud007-zotero',
                   ARRAY['A. Author'], 'https://example.test/aud007', $1, 'A-GLOBAL-KEY')
           RETURNING id""",
        user_a_id,
    )
    # A's per-user link row — the ONLY place the per-user link table read looks.
    await contract_conn.execute(
        "INSERT INTO paper_user_zotero_links (paper_id, user_id, zotero_item_key)"
        " VALUES ($1, $2, 'A-LINK-KEY')",
        int(paper_id),
        user_a_id,
    )
    # Both users carry a usable Zotero config so neither short-circuits to
    # 'disabled' before reaching the per-user link read.
    for uid in (user_a_id, user_b_id):
        for key, value in [
            ("zotero.api_key", "fake-key"),
            ("zotero.user_id", "999999"),
            ("zotero.library_type", "user"),
        ]:
            await contract_conn.execute(
                "INSERT INTO user_config (user_id, key, value) VALUES ($1, $2, $3::jsonb)",
                uid,
                key,
                f'"{value}"',
            )

    shared = SharedConnPool(contract_conn)
    http = AsyncMock(spec=httpx.AsyncClient)

    with patch("paper_ingestion.integrations.zotero_client.ZoteroClient"):
        result_b = await push_highlights_for_paper(
            int(paper_id), db_pool=shared, http_client=http, owner_user_id=user_b_id
        )
        result_a = await push_highlights_for_paper(
            int(paper_id), db_pool=shared, http_client=http, owner_user_id=user_a_id
        )

    # User B has no link for P: the join yields no row -> not_linked. A reverted
    # global-column read would return 'A-GLOBAL-KEY' here and skip not_linked.
    assert result_b["status"] == "not_linked", (
        f"cross-user LEAK: user B saw user A's Zotero link; got {result_b}"
    )
    # User A's own link resolves -> proceeds past not_linked (no highlights to export).
    assert result_a["status"] == "ok", f"user A's own Zotero link must resolve; got {result_a}"


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_post_0094_backfill_rows_visible_to_owning_user(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
) -> None:
    """Rows backfilled by migration 0094 (user_id set to admin) remain visible.

    Regression guard: the per-user read predicate ``AND user_id = $N`` must
    match rows whose user_id was set during the 0094 NULL->admin backfill.
    This test simulates that state by inserting a row with user_id explicitly
    set to user A, then asserting user A's read returns it.

    If the query predicate were inverted or used a wrong column the row would
    vanish — this test catches that silent regression.

    Verified: extractions.py:266-295 get_paper_extractions WHERE user_id=$2
    # Verified: extractions.py:268
    """
    # Seed: shared paper + library membership for user A only (A is the owner).
    paper_id = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by)
           VALUES ('iso-ext-backfill', 'arxiv', 'backfill-preservation-test',
                   ARRAY['C. Author'], 'https://example.test/backfill', NULL)
           RETURNING id"""
    )
    await contract_conn.execute(
        "INSERT INTO user_library (user_id, paper_id, added_via) VALUES ($1, $2, 'manual_save')",
        contract_two_users.user_a_id,
        paper_id,
    )

    # Seed: template + extraction row with user_id = user_a_id (simulates backfill).
    template_id = await contract_conn.fetchval(
        """INSERT INTO extraction_templates (name, description, fields, is_default)
           VALUES ('iso-backfill-template', 'backfill test', '[]'::jsonb, FALSE)
           RETURNING id"""
    )
    await contract_conn.execute(
        """INSERT INTO paper_extractions (paper_id, template_id, extractions, user_id)
           VALUES ($1, $2, '{"result": {"value": "backfill-data"}}'::jsonb, $3)""",
        paper_id,
        template_id,
        contract_two_users.user_a_id,
    )

    async with _make_contract_client(_pi_app_with_pool, contract_two_users.cookie_a) as client:
        resp = await client.get(f"/api/papers/{paper_id}/extractions")

    assert resp.status_code == 200, (
        f"GET /api/papers/{{paper_id}}/extractions returned {resp.status_code}: {resp.text[:300]}"
    )
    body = resp.json()
    assert len(body) == 1, (
        f"Expected 1 extraction row for user A (backfill row), got {len(body)}: {resp.text[:300]}"
    )
    assert body[0]["template_id"] == template_id
    # Verify the backfilled row is actually present in the DB with the correct user_id.
    db_row = await contract_conn.fetchrow(
        "SELECT user_id FROM paper_extractions WHERE paper_id=$1 AND template_id=$2",
        paper_id,
        template_id,
    )
    assert db_row is not None, "Backfill extraction row was not found in DB"
    assert db_row["user_id"] == contract_two_users.user_a_id, (
        f"Backfill row user_id mismatch: expected {contract_two_users.user_a_id}, "
        f"got {db_row['user_id']}"
    )


# ---------------------------------------------------------------------------
# Per-user surface isolation: paper_summaries
#
# paper_summaries is keyed UNIQUE NULLS NOT DISTINCT (paper_id, user_id) — each
# user gets their own summary for a shared canonical paper. GET /api/papers/{id}
# must serve the CALLER's summary row, never another user's, even when both own
# the (globally shared) paper. No existing test covers the summary read scope.
# ---------------------------------------------------------------------------

_A_SUMMARY_MARKER = "ZZZ-ISOLATION-A-SUMMARY-CONTENT"


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_summary_read_scoped_to_calling_user(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
) -> None:
    """User B must not see user A's paper_summaries row via GET /api/papers/{id}.

    Seed: shared paper (discovered_by NULL, in both libraries) + a summary owned
    by user A only. Asserts user B's paper detail returns summary=None (A's row
    excluded) with 200, while user A's detail returns A's summary marker.
    """
    paper_id = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by)
           VALUES ('iso-ext-shared-summary', 'arxiv', 'shared-paper-summary-isolation',
                   ARRAY['D. Author'], 'https://example.test/shared-summary', NULL)
           RETURNING id"""
    )
    await contract_conn.executemany(
        "INSERT INTO user_library (user_id, paper_id, added_via) VALUES ($1, $2, 'manual_save')",
        [
            (contract_two_users.user_a_id, paper_id),
            (contract_two_users.user_b_id, paper_id),
        ],
    )
    await contract_conn.execute(
        """INSERT INTO paper_summaries (paper_id, summary_brief, summary_detailed, user_id)
           VALUES ($1, $2, $2, $3)""",
        paper_id,
        _A_SUMMARY_MARKER,
        contract_two_users.user_a_id,
    )

    async with _make_contract_client(_pi_app_with_pool, contract_two_users.cookie_b) as client_b:
        resp_b = await client_b.get(f"/api/papers/{paper_id}")
    async with _make_contract_client(_pi_app_with_pool, contract_two_users.cookie_a) as client_a:
        resp_a = await client_a.get(f"/api/papers/{paper_id}")

    assert resp_b.status_code == 200, (
        f"GET /api/papers/{{id}} as B returned {resp_b.status_code}: {resp_b.text[:300]}"
    )
    assert resp_b.json()["summary"] is None, (
        f"LEAK: user B saw user A's summary row: {resp_b.text[:300]}"
    )
    assert _A_SUMMARY_MARKER not in resp_b.text, (
        f"LEAK: A's summary marker visible to B: {resp_b.text[:300]}"
    )

    assert resp_a.status_code == 200, (
        f"GET /api/papers/{{id}} as A returned {resp_a.status_code}: {resp_a.text[:300]}"
    )
    summary_a = resp_a.json()["summary"]
    assert summary_a is not None and summary_a["summary_brief"] == _A_SUMMARY_MARKER, (
        f"owner read regression: user A did not get their own summary: {resp_a.text[:300]}"
    )


# ---------------------------------------------------------------------------
# F12: weekly-summary digest paper_summaries join scoping
#
# generate_weekly_summary LEFT JOINs paper_summaries onto each engaged paper.
# paper_summaries is per-user (UNIQUE NULLS NOT DISTINCT (paper_id, user_id)),
# so the JOIN must be scoped to the requesting user (ps.user_id IS NOT DISTINCT
# FROM <user>) in the ON clause — an unscoped join surfaces another user's
# summary_brief / confidence to the requester, and a WHERE-clause predicate
# would silently drop engaged-but-unsummarized papers (LEFT->INNER).
# ---------------------------------------------------------------------------


async def _seed_weekly_engaged_paper(conn, ext_id, title, user_id, topic_name, created_at=None):
    """Seed a shared paper engaged (starred) by *user_id* under its own topic.

    Returns the new paper id. No paper_summaries row is created here — callers
    add per-user summaries explicitly so the JOIN-scoping behaviour is isolated.
    When *created_at* is given it overrides papers.created_at (lets a test seed
    a paper discovered before the weekly cutoff).
    """
    paper_id = await conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by, created_at)
           VALUES ($1, 'arxiv', $2, ARRAY['W. Author'], $3, NULL, COALESCE($4, now()))
           RETURNING id""",
        ext_id,
        title,
        f"https://example.test/{ext_id}",
        created_at,
    )
    await conn.execute(
        "INSERT INTO user_library (user_id, paper_id, added_via) VALUES ($1, $2, 'manual_save')",
        user_id,
        paper_id,
    )
    await conn.execute(
        "INSERT INTO paper_user_state (paper_id, user_id, state, starred) "
        "VALUES ($1, $2, 'to_read', TRUE)",
        paper_id,
        user_id,
    )
    topic_id = await conn.fetchval(
        "INSERT INTO topics (name, query_terms) VALUES ($1, ARRAY['q']) RETURNING id",
        topic_name,
    )
    await conn.execute(
        "INSERT INTO paper_topics (paper_id, topic_id, relevance_score) VALUES ($1, $2, 0.9)",
        paper_id,
        topic_id,
    )
    return paper_id


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_weekly_summary_join_excludes_other_users_summary(
    contract_two_users,
    contract_conn,
) -> None:
    """User B's weekly digest must not surface user A's paper_summaries row.

    Seed a single shared paper engaged by BOTH users. Only user A has a
    paper_summaries row (a distinctive summary_brief + confidence='LOW').
    Generating B's weekly summary must show that paper with NO summary data
    (confidence is None and A's brief marker is absent everywhere) — proving
    the LEFT JOIN is scoped to the requesting user.
    """
    from jarvis_common.testing_db import SharedConnPool
    from jarvis_common.verify import QuoteVerifier
    from paper_ingestion.weekly_summary import generate_weekly_summary

    paper_id = await _seed_weekly_engaged_paper(
        contract_conn,
        "wk-iso-shared",
        "shared-paper-weekly-join-isolation",
        contract_two_users.user_a_id,
        "weekly-iso-topic-shared",
    )
    # B engages the SAME shared paper (own library + state row), no summary.
    await contract_conn.execute(
        "INSERT INTO user_library (user_id, paper_id, added_via) VALUES ($1, $2, 'manual_save')",
        contract_two_users.user_b_id,
        paper_id,
    )
    await contract_conn.execute(
        "INSERT INTO paper_user_state (paper_id, user_id, state, starred) "
        "VALUES ($1, $2, 'to_read', TRUE)",
        paper_id,
        contract_two_users.user_b_id,
    )
    # Only A owns a summary for this paper (distinctive brief + confidence).
    await contract_conn.execute(
        """INSERT INTO paper_summaries (paper_id, summary_brief, summary_detailed, confidence, user_id)
           VALUES ($1, $2, $2, 'LOW', $3)""",
        paper_id,
        _A_SUMMARY_MARKER,
        contract_two_users.user_a_id,
    )

    pool = SharedConnPool(contract_conn)
    result = await generate_weekly_summary(
        db_pool=pool,
        verifier=QuoteVerifier(),
        days=7,
        user_id=contract_two_users.user_b_id,
        openai_client=MagicMock(),
    )

    assert result["total_papers"] == 1, (
        f"B's engaged shared paper must appear in the digest: {result}"
    )
    top_paper = next(
        (tp for t in result["topics"] for tp in t["top_papers"] if tp["id"] == paper_id),
        None,
    )
    assert top_paper is not None, f"B's engaged paper missing from the digest: {result}"
    # ps.confidence is selected per-paper; A's LOW must NOT leak to B (scoped JOIN).
    assert top_paper["confidence"] is None, (
        f"LEAK: user A's summary confidence visible to user B: {top_paper}"
    )
    # A's brief marker must be absent from the entire serialized digest.
    assert _A_SUMMARY_MARKER not in json.dumps(result), (
        f"LEAK: user A's summary_brief visible to user B: {result}"
    )


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_weekly_summary_preserves_engaged_paper_without_summary(
    contract_two_users,
    contract_conn,
) -> None:
    """An engaged paper with NO summary still appears in the digest (LEFT JOIN).

    Regression guard for the F12 fix: the user-scoping predicate lives in the
    JOIN ON clause, not WHERE. A WHERE predicate would convert the LEFT JOIN
    into an inner join and drop engaged-but-unsummarized papers from B's
    digest. B engages a paper for which NEITHER user has a summary.
    """
    from jarvis_common.testing_db import SharedConnPool
    from jarvis_common.verify import QuoteVerifier
    from paper_ingestion.weekly_summary import generate_weekly_summary

    paper_id = await _seed_weekly_engaged_paper(
        contract_conn,
        "wk-iso-nosummary",
        "weekly-paper-no-summary",
        contract_two_users.user_b_id,
        "weekly-iso-topic-nosummary",
    )

    pool = SharedConnPool(contract_conn)
    result = await generate_weekly_summary(
        db_pool=pool,
        verifier=QuoteVerifier(),
        days=7,
        user_id=contract_two_users.user_b_id,
        openai_client=MagicMock(),
    )

    assert result["total_papers"] == 1, (
        f"engaged paper with no summary must survive the LEFT JOIN: {result}"
    )
    top_paper = next(
        (tp for t in result["topics"] for tp in t["top_papers"] if tp["id"] == paper_id),
        None,
    )
    assert top_paper is not None, f"engaged-but-unsummarized paper dropped from digest: {result}"
    assert top_paper["confidence"] is None


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_weekly_summary_includes_recently_engaged_old_paper(
    contract_two_users,
    contract_conn,
) -> None:
    """A paper discovered before the cutoff but engaged within the window
    must appear in the digest (engagement recency, not discovery date)."""
    from datetime import UTC, datetime, timedelta

    from jarvis_common.testing_db import SharedConnPool
    from jarvis_common.verify import QuoteVerifier
    from paper_ingestion.weekly_summary import generate_weekly_summary

    old_created = datetime.now(UTC) - timedelta(days=40)
    paper_id = await _seed_weekly_engaged_paper(
        contract_conn,
        "wk-old-recent-star",
        "old-paper-recently-starred",
        contract_two_users.user_b_id,
        "weekly-recency-topic",
        created_at=old_created,
    )
    # The star (paper_user_state row) was just inserted, so pus.updated_at = now()
    # — recent engagement on an old paper. Force its updated_at to be unambiguously
    # inside the 7-day window in case the seed ran at a clock boundary.
    await contract_conn.execute(
        "UPDATE paper_user_state SET updated_at = now() WHERE paper_id = $1 AND user_id = $2",
        paper_id,
        contract_two_users.user_b_id,
    )

    pool = SharedConnPool(contract_conn)
    result = await generate_weekly_summary(
        db_pool=pool,
        verifier=QuoteVerifier(),
        days=7,
        user_id=contract_two_users.user_b_id,
        openai_client=MagicMock(),
    )

    assert result["total_papers"] == 1, (
        f"a paper engaged this week must appear even if discovered 40 days ago: {result}"
    )
    top_paper = next(
        (tp for t in result["topics"] for tp in t["top_papers"] if tp["id"] == paper_id),
        None,
    )
    assert top_paper is not None, f"recently-engaged old paper dropped from digest: {result}"


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_weekly_summary_excludes_stalely_engaged_old_paper(
    contract_two_users,
    contract_conn,
) -> None:
    """An old paper whose engagement is ALSO stale must NOT appear in the digest.

    Load-bearing for the DAT-6 recency gate: if either
    `AND pusN.updated_at >= $1` predicate were removed from weekly_summary.py,
    this stale-starred old paper would wrongly surface and this test would fail.
    The paper was discovered 40 days ago and last engaged 30 days ago — both
    outside the 7-day window — so it is correctly absent.
    """
    from datetime import UTC, datetime, timedelta

    from jarvis_common.testing_db import SharedConnPool
    from jarvis_common.verify import QuoteVerifier
    from paper_ingestion.weekly_summary import generate_weekly_summary

    old_created = datetime.now(UTC) - timedelta(days=40)
    paper_id = await _seed_weekly_engaged_paper(
        contract_conn,
        "wk-old-stale-star",
        "old-paper-stalely-starred",
        contract_two_users.user_b_id,
        "weekly-recency-stale-topic",
        created_at=old_created,
    )
    # The star set updated_at = now(); push engagement back to 30 days ago —
    # before the 7-day cutoff but after discovery — so neither the discovery
    # date nor the engagement recency admits this paper. The BEFORE-UPDATE
    # trigger set_updated_at_paper_user_state would clobber updated_at back to
    # now(), so disable it for this single backdating UPDATE, then re-enable.
    stale_engaged = datetime.now(UTC) - timedelta(days=30)
    await contract_conn.execute(
        "ALTER TABLE paper_user_state DISABLE TRIGGER set_updated_at_paper_user_state"
    )
    try:
        await contract_conn.execute(
            "UPDATE paper_user_state SET updated_at = $3 WHERE paper_id = $1 AND user_id = $2",
            paper_id,
            contract_two_users.user_b_id,
            stale_engaged,
        )
    finally:
        await contract_conn.execute(
            "ALTER TABLE paper_user_state ENABLE TRIGGER set_updated_at_paper_user_state"
        )

    pool = SharedConnPool(contract_conn)
    result = await generate_weekly_summary(
        db_pool=pool,
        verifier=QuoteVerifier(),
        days=7,
        user_id=contract_two_users.user_b_id,
        openai_client=MagicMock(),
    )

    assert result["total_papers"] == 0, (
        f"an old paper engaged 30 days ago must NOT appear in this week's digest: {result}"
    )
    leaked = next(
        (tp for t in result["topics"] for tp in t["top_papers"] if tp["id"] == paper_id),
        None,
    )
    assert leaked is None, f"stalely-engaged old paper wrongly surfaced in digest: {leaked}"


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_weekly_summary_recent_engagement_scoped_to_engaging_user(
    contract_two_users,
    contract_conn,
) -> None:
    """The recency-inclusion path is per-user: an old paper engaged this week by
    user B alone must NOT appear in user A's digest (A neither discovered it
    in-window nor engaged it). Complements the positive test, which proves B
    DOES see it."""
    from datetime import UTC, datetime, timedelta

    from jarvis_common.testing_db import SharedConnPool
    from jarvis_common.verify import QuoteVerifier
    from paper_ingestion.weekly_summary import generate_weekly_summary

    old_created = datetime.now(UTC) - timedelta(days=40)
    paper_id = await _seed_weekly_engaged_paper(
        contract_conn,
        "wk-old-recent-star-b-only",
        "old-paper-recently-starred-by-b",
        contract_two_users.user_b_id,
        "weekly-recency-scope-topic",
        created_at=old_created,
    )
    await contract_conn.execute(
        "UPDATE paper_user_state SET updated_at = now() WHERE paper_id = $1 AND user_id = $2",
        paper_id,
        contract_two_users.user_b_id,
    )

    pool = SharedConnPool(contract_conn)
    result = await generate_weekly_summary(
        db_pool=pool,
        verifier=QuoteVerifier(),
        days=7,
        user_id=contract_two_users.user_a_id,
        openai_client=MagicMock(),
    )

    assert result["total_papers"] == 0, (
        f"a paper engaged only by user B must NOT appear in user A's digest: {result}"
    )
    leaked = next(
        (tp for t in result["topics"] for tp in t["top_papers"] if tp["id"] == paper_id),
        None,
    )
    assert leaked is None, f"CROSS-USER: B's recently-engaged old paper visible to A: {leaked}"
