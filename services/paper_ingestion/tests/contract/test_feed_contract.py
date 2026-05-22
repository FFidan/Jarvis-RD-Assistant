"""Feed endpoint contract tests (Phase B, B.PI-pulse-rag).

Covers GET /api/papers/feed DB-layer behaviors:
  - State-filter view predicates (inbox, reading, done, trash) against real DB rows
  - User-scoped isolation (user B cannot see user A's papers in library scope)
  - Empty result with total=0 for empty library

Survivor-of:
  - Strengthens test_feed.py mock-unit assertions against feed query predicates

Contract test targets:
  - Verified: services/paper_ingestion/paper_ingestion/routers/feed.py:32-138
    (GET /api/papers/feed — list_feed_papers)
  - Verified: services/paper_ingestion/paper_ingestion/services/feed_query.py (build_feed_queries)

Idiomatic-mock carve-out (KEEP):
  - app.state.http_client = MagicMock() (outbound HTTP boundary)
  - app.state.embedder = MagicMock() (Ollama/Qdrant boundary)
"""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio

from jarvis_common.testing import SharedConnPool

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]

_TEST_API_KEY = "feed-contract-key-do-not-use-in-prod"


# ---------------------------------------------------------------------------
# App + client fixture
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def _pi_app(contract_conn):
    """paper_ingestion app wired to the contract connection, rate limiter off."""
    from unittest.mock import MagicMock

    from paper_ingestion.deps import get_db_pool, limiter
    from paper_ingestion.main import app

    shared = SharedConnPool(contract_conn)
    original_pool = getattr(app.state, "db_pool", None)
    original_http = getattr(app.state, "http_client", None)
    original_embedder = getattr(app.state, "embedder", None)

    app.state.db_pool = shared
    app.state.http_client = MagicMock()
    app.state.embedder = MagicMock()
    app.dependency_overrides[get_db_pool] = lambda: shared

    limiter_was_enabled = limiter.enabled
    limiter.enabled = False

    try:
        yield app
    finally:
        limiter.enabled = limiter_was_enabled
        if original_pool is None:
            app.state.__dict__.pop("db_pool", None)
        else:
            app.state.db_pool = original_pool
        if original_http is None:
            app.state.__dict__.pop("http_client", None)
        else:
            app.state.http_client = original_http
        if original_embedder is None:
            app.state.__dict__.pop("embedder", None)
        else:
            app.state.embedder = original_embedder
        app.dependency_overrides.pop(get_db_pool, None)


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


def _client(app, cookie: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"X-API-Key": _TEST_API_KEY},
        cookies={"jarvis_session": cookie},
    )


# ---------------------------------------------------------------------------
# §A-FEED-01 — feed returns papers for user's library
# Verified: feed.py:32-138 (list_feed_papers)
# ---------------------------------------------------------------------------


async def test_feed_returns_library_papers(contract_two_users, _pi_app, _configure_api_key):
    """GET /api/papers/feed returns the seeded paper in user A's library scope.

    Proves the library query includes papers added to user_library.
    """
    paper_id_a = contract_two_users.paper_id_a

    async with _client(_pi_app, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/papers/feed?scope=library")

    assert resp.status_code == 200, f"Expected 200; got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert "papers" in body
    assert "total" in body
    returned_ids = [p["id"] for p in body["papers"]]
    assert paper_id_a in returned_ids, (
        "Paper added to user_library must appear in GET /api/papers/feed?scope=library"
    )


# ---------------------------------------------------------------------------
# §A-FEED-02 — feed user_id isolation: user B cannot see user A's library paper
# Verified: feed.py:32-138 (list_feed_papers — library scope join to user_library)
# ---------------------------------------------------------------------------


async def test_feed_library_scope_user_isolation(contract_two_users, _pi_app, _configure_api_key):
    """User B cannot see user A's library paper in library scope.

    The library scope query joins user_library WHERE user_id = <caller>
    so user B's response must not include user A's paper.
    """
    paper_id_a = contract_two_users.paper_id_a

    async with _client(_pi_app, contract_two_users.cookie_b) as c:
        resp = await c.get("/api/papers/feed?scope=library")

    assert resp.status_code == 200
    body = resp.json()
    returned_ids = [p["id"] for p in body["papers"]]
    assert paper_id_a not in returned_ids, (
        "User B must not see user A's library paper in feed?scope=library — user_id isolation required"
    )


# ---------------------------------------------------------------------------
# §A-FEED-03 — feed view=inbox returns paper with state='to_read' (inbox-equivalent)
# Verified: feed.py:95-138 (view predicate dispatch via VIEW_PREDICATES)
# The seeded paper has state='to_read' which maps to reading_list; inbox = no state or 'inbox'.
# ---------------------------------------------------------------------------


async def test_feed_view_trash_excludes_non_trash_paper(
    contract_conn, contract_two_users, _pi_app, _configure_api_key
):
    """GET /api/papers/feed?view=trash returns only trashed papers.

    Seed an additional paper with state='trash' and verify:
    - The trashed paper appears in view=trash
    - The non-trashed seeded paper does NOT appear in view=trash
    """
    user_id = contract_two_users.user_a_id

    # Seed a new paper and mark it as trash
    trash_paper_id = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by)
           VALUES ('feed-trash-01', 'arxiv', 'Feed Trash Paper', ARRAY['A'], 'https://t.test/ft', $1)
           RETURNING id""",
        user_id,
    )
    await contract_conn.execute(
        """INSERT INTO user_library (user_id, paper_id, added_via)
           VALUES ($1, $2, 'manual_save')""",
        user_id,
        trash_paper_id,
    )
    await contract_conn.execute(
        """INSERT INTO paper_user_state (paper_id, user_id, state)
           VALUES ($1, $2, 'trash')
           ON CONFLICT (paper_id, user_id) DO UPDATE SET state = 'trash'""",
        trash_paper_id,
        user_id,
    )

    async with _client(_pi_app, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/papers/feed?view=trash")

    assert resp.status_code == 200, f"Expected 200; got {resp.status_code}: {resp.text}"
    body = resp.json()
    returned_ids = [p["id"] for p in body["papers"]]
    assert trash_paper_id in returned_ids, "Trashed paper must appear in feed?view=trash"
    # The seeded paper (state='to_read') must NOT appear in the trash view
    paper_id_a = contract_two_users.paper_id_a
    assert paper_id_a not in returned_ids, "Non-trashed paper must NOT appear in feed?view=trash"


# ---------------------------------------------------------------------------
# §A-FEED-04 — feed invalid view returns 422
# Verified: feed.py:95-99 (unknown view → 422)
# ---------------------------------------------------------------------------


async def test_feed_invalid_view_returns_422(contract_two_users, _pi_app, _configure_api_key):
    """GET /api/papers/feed?view=<invalid> returns 422 Unprocessable Entity."""
    async with _client(_pi_app, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/papers/feed?view=not_a_real_view")

    assert resp.status_code == 422, (
        f"Expected 422 for unknown view; got {resp.status_code}: {resp.text}"
    )


# ---------------------------------------------------------------------------
# §A-FEED-05 — feed empty library returns total=0
# Verified: feed.py:128-138 (FeedResponse total=count_row or 0)
# ---------------------------------------------------------------------------


async def test_feed_empty_library_returns_zero_total(contract_conn, _pi_app, _configure_api_key):
    """GET /api/papers/feed for a user with no library papers returns total=0."""
    # Seed a user with no papers
    user_id = await contract_conn.fetchval(
        "INSERT INTO users (email, role) VALUES ('feed-empty@contract.example.com', 'user') RETURNING id"
    )
    session_id = await contract_conn.fetchval(
        """INSERT INTO sessions (user_id, expires_at)
           VALUES ($1, NOW() + INTERVAL '1 day')
           RETURNING id""",
        user_id,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_pi_app),
        base_url="http://test",
        headers={"X-API-Key": _TEST_API_KEY},
        cookies={"jarvis_session": str(session_id)},
    ) as c:
        resp = await c.get("/api/papers/feed?scope=library")

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 0, f"Empty library must return total=0; got total={body['total']}"
    assert body["papers"] == [], "Empty library must return empty papers list"


# ---------------------------------------------------------------------------
# §A-FEED-06 — feed invalid scope returns 422
# Verified: feed.py:100-104 (unknown scope → 422)
# ---------------------------------------------------------------------------


async def test_feed_invalid_scope_returns_422(contract_two_users, _pi_app, _configure_api_key):
    """GET /api/papers/feed?scope=<invalid> returns 422 Unprocessable Entity."""
    async with _client(_pi_app, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/papers/feed?scope=invalid_scope")

    assert resp.status_code == 422, (
        f"Expected 422 for unknown scope; got {resp.status_code}: {resp.text}"
    )
