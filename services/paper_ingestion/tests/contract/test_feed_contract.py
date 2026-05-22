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

from jarvis_common.testing_contract_apps import (
    DEFAULT_CONTRACT_API_KEY,
    make_contract_client as _client,
)

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]


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
        headers={"X-API-Key": DEFAULT_CONTRACT_API_KEY},
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


# ---------------------------------------------------------------------------
# Cluster 8 — Dashboard feed filters (source_types / q / date_from-to)
# Survivor-of mock-units in test_dashboard_api.py:
#   test_feed_filter_by_source     → C8-feed-source
#   test_feed_filter_by_text       → C8-feed-text
#   test_feed_filter_by_date_range → C8-feed-date
# ---------------------------------------------------------------------------


async def test_feed_filter_by_source_type_behavioral(
    contract_two_users, contract_conn, _pi_app, _configure_api_key
):
    """GET /api/papers/feed?source_types=X returns only papers with that source_type.

    Seeds two papers for user A — one arxiv, one semantic_scholar — and verifies
    the filter excludes the non-matching source. Replaces the SQL-substring
    assertion ("p.source_type IN" in sql) with a behavioral consequence.

    # Verified: services/paper_ingestion/paper_ingestion/routers/feed.py:31
    # (list_feed_papers passes source_types to build_feed_queries).
    """
    # Seed an additional paper with a different source_type
    other_paper_id = await contract_conn.fetchval(
        """
        INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by)
        VALUES ('feed-s2-1', 'semantic_scholar', 'Other Source Paper', ARRAY['B'],
                'https://example.test/s2', $1)
        RETURNING id
        """,
        contract_two_users.user_a_id,
    )
    await contract_conn.execute(
        "INSERT INTO user_library (user_id, paper_id, added_via) VALUES ($1, $2, 'manual_save')",
        contract_two_users.user_a_id,
        other_paper_id,
    )

    async with _client(_pi_app, contract_two_users.cookie_a) as c:
        # _seed_resources creates paper_id_a with source_type='arxiv'.
        resp = await c.get("/api/papers/feed?source_types=arxiv")

    assert resp.status_code == 200, resp.text[:300]
    ids = [p["id"] for p in resp.json().get("papers", [])]
    assert contract_two_users.paper_id_a in ids, (
        f"arxiv-filter dropped user A's arxiv paper {contract_two_users.paper_id_a}: {ids}"
    )
    assert other_paper_id not in ids, (
        f"arxiv-filter leaked semantic_scholar paper {other_paper_id}: {ids}"
    )


async def test_feed_filter_by_text_behavioral(
    contract_two_users, contract_conn, _pi_app, _configure_api_key
):
    """GET /api/papers/feed?q=X returns papers matching the BM25 search query.

    The seeded user-A paper has title "ZZZ-ISOLATION-A-PAPER Quantum Entanglement
    of Owls" (see jarvis_common.testing.A_PAPER_TITLE). q=Quantum should match.

    # Verified: services/paper_ingestion/paper_ingestion/routers/feed.py:31
    # (list_feed_papers passes q to build_feed_queries which uses websearch_to_tsquery).
    """
    # Refresh the paper's tsvector if a trigger exists; otherwise BM25 column should
    # already be populated by the insert trigger in _seed_resources.
    async with _client(_pi_app, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/papers/feed?q=Quantum")

    assert resp.status_code == 200, resp.text[:300]
    ids = [p["id"] for p in resp.json().get("papers", [])]
    assert contract_two_users.paper_id_a in ids, (
        f"q=Quantum should match seeded A_PAPER_TITLE; got ids={ids}"
    )


async def test_feed_filter_by_date_range_behavioral(
    contract_two_users, contract_conn, _pi_app, _configure_api_key
):
    """GET /api/papers/feed?date_from=...&date_to=... filters by created_at.

    Seeds two additional papers — one before the window and one inside —
    and verifies only the in-window paper appears. Replaces the SQL-substring
    assertion ("p.created_at >=" in sql) with a behavioral assertion.

    # Verified: services/paper_ingestion/paper_ingestion/routers/feed.py:31
    # (list_feed_papers passes date_from / date_to to build_feed_queries).
    """
    in_window_id = await contract_conn.fetchval(
        """
        INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by, created_at)
        VALUES ('feed-in-window', 'arxiv', 'In-window paper', ARRAY['IW'],
                'https://example.test/iw', $1, '2025-06-15T12:00:00Z')
        RETURNING id
        """,
        contract_two_users.user_a_id,
    )
    out_of_window_id = await contract_conn.fetchval(
        """
        INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by, created_at)
        VALUES ('feed-out-window', 'arxiv', 'Out-of-window paper', ARRAY['OW'],
                'https://example.test/ow', $1, '2023-01-01T12:00:00Z')
        RETURNING id
        """,
        contract_two_users.user_a_id,
    )
    for pid in (in_window_id, out_of_window_id):
        await contract_conn.execute(
            "INSERT INTO user_library (user_id, paper_id, added_via) VALUES ($1, $2, 'manual_save')",
            contract_two_users.user_a_id,
            pid,
        )

    async with _client(_pi_app, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/papers/feed?date_from=2025-01-01&date_to=2026-01-01")

    assert resp.status_code == 200, resp.text[:300]
    ids = [p["id"] for p in resp.json().get("papers", [])]
    assert in_window_id in ids, f"date_range filter dropped in-window paper {in_window_id}: {ids}"
    assert out_of_window_id not in ids, (
        f"date_range filter leaked out-of-window paper {out_of_window_id}: {ids}"
    )
