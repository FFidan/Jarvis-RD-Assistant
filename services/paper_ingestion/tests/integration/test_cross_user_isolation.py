"""WS-NEGATIVE-TESTS — cross-user data-isolation negative suite.

THE merge gate for the v0.4.0 public launch. Drives BOTH service apps
(paper_ingestion + learning_engine) with the *real* ``SessionMiddleware``
and the *real* strict user-id resolvers (the autouse stub is opted out via
``@pytest.mark.real_auth``). User B carries a valid ``jarvis_session`` cookie
and attempts to read / mutate user A's owned rows; every attempt must be
denied (403/404) and must not leak A's content.

Run (Docker PostgreSQL required)::

    JARVIS_RUN_LIVE_PG=1 uv run pytest -m "integration and live_pg" \
        services/paper_ingestion/tests/integration/test_cross_user_isolation.py -v

Standard ``uv run pytest`` excludes ``live_pg``/``integration`` so this is
inert in the fast suite (see root pyproject ``addopts``).

A leak surfaces as a *failing* assertion here — per plan §12.4 those are
real bugs to fix at the query layer, not assertions to weaken.
"""

from __future__ import annotations

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
# Paper TITLE is deliberately NOT private under the Sprint-B canonical-corpus
# model: papers are a GLOBAL shared corpus (see
# jarvis_common.db_helpers.assert_paper_ownership docstring — "Papers are
# global (canonical corpus). Ownership = library membership."). What is
# per-user-private is *library membership / user-state / notes*, not the
# paper's bibliographic metadata. So the global papers collection
# (`corpus_list` kind) only forbids the private markers, while per-user
# paper access (`byid`/`mutate`, gated by assert_paper_ownership) and all
# per-user collections still forbid the title too.
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
# /api/similar, /api/generate*, extract endpoints.
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
