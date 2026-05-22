"""Recommendations eligibility-filter contract tests (Phase B, B.PI-pulse-rag).

Replaces the B1-09 SQL-substring cluster in test_recommender.py lines 186-223
(TestFilterUnread) with real asyncpg assertions against the live schema.

Survivor-of:
  - test_recommender.py::TestFilterUnread::test_filter_unread_excludes_trash_papers
  - test_recommender.py::TestFilterUnread::test_filter_unread_excludes_done_papers
  - test_recommender.py::TestFilterUnread::test_filter_unread_excludes_negative_feedback_within_60_days
  - test_recommender.py::TestFilterUnread::test_filter_unread_includes_negative_feedback_older_than_60_days
  - test_recommender.py::TestFilterUnread::test_filter_unread_includes_papers_with_no_state
  - test_recommender.py::TestFilterUnread::test_starred_papers_remain_eligible_for_recommendation

Contract test targets:
  - Verified: services/paper_ingestion/paper_ingestion/ingestion/recommender.py:220-245
    (_filter_unread: asyncpg.Connection, list[int], int → set[int])
  - Verified: services/paper_ingestion/paper_ingestion/routers/recommendations.py:22-39
    (GET /api/recommendations — dismissed=FALSE scope + user_id isolation)
  - Verified: services/paper_ingestion/paper_ingestion/routers/recommendations.py:51-68
    (POST /api/recommendations/{paper_id}/dismiss)

Idiomatic-mock carve-out (KEEP):
  - All Qdrant, Ollama, and embedder calls stay mocked in the full refresh path.
  - _filter_unread itself is pure asyncpg — no external mocks needed here.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from jarvis_common.testing import SharedConnPool

from jarvis_common.testing_contract_apps import (
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
# §A-RECS-01 — _filter_unread: trash state exclusion
# Verified: recommender.py:220-245 (_filter_unread)
# Survivor-of: test_recommender.py::TestFilterUnread::test_filter_unread_excludes_trash_papers
# ---------------------------------------------------------------------------


async def test_filter_unread_excludes_trash_papers(contract_conn):
    """_filter_unread must NOT return papers whose state = 'trash'.

    Exercises real SQL predicate: NOT EXISTS (SELECT 1 FROM paper_user_state
    WHERE ... AND COALESCE(pus.state, 'inbox') IN ('trash', 'done'))
    against a real paper + user_state row.
    """
    from paper_ingestion.ingestion.recommender import _filter_unread

    user_id = await contract_conn.fetchval(
        "INSERT INTO users (email, role) VALUES ('recs-trash@contract.example.com', 'user') RETURNING id"
    )
    paper_id = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url)
           VALUES ('recs-trash-01', 'arxiv', 'Trashed Paper', '{}', 'https://t.test/trash')
           RETURNING id"""
    )
    await contract_conn.execute(
        """INSERT INTO paper_user_state (paper_id, user_id, state)
           VALUES ($1, $2, 'trash')
           ON CONFLICT (paper_id, user_id) DO UPDATE SET state = 'trash'""",
        paper_id,
        user_id,
    )

    result = await _filter_unread(contract_conn, [paper_id], user_id=user_id)
    assert paper_id not in result, (
        "Paper with state='trash' must be excluded from _filter_unread "
        "(COALESCE(pus.state, 'inbox') IN ('trash', 'done') guard)"
    )


# ---------------------------------------------------------------------------
# §A-RECS-02 — _filter_unread: done state exclusion
# Verified: recommender.py:220-245 (_filter_unread)
# Survivor-of: test_recommender.py::TestFilterUnread::test_filter_unread_excludes_done_papers
# ---------------------------------------------------------------------------


async def test_filter_unread_excludes_done_papers(contract_conn):
    """_filter_unread must NOT return papers whose state = 'done'."""
    from paper_ingestion.ingestion.recommender import _filter_unread

    user_id = await contract_conn.fetchval(
        "INSERT INTO users (email, role) VALUES ('recs-done@contract.example.com', 'user') RETURNING id"
    )
    paper_id = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url)
           VALUES ('recs-done-01', 'arxiv', 'Done Paper', '{}', 'https://t.test/done')
           RETURNING id"""
    )
    await contract_conn.execute(
        """INSERT INTO paper_user_state (paper_id, user_id, state)
           VALUES ($1, $2, 'done')
           ON CONFLICT (paper_id, user_id) DO UPDATE SET state = 'done'""",
        paper_id,
        user_id,
    )

    result = await _filter_unread(contract_conn, [paper_id], user_id=user_id)
    assert paper_id not in result, "Paper with state='done' must be excluded from _filter_unread"


# ---------------------------------------------------------------------------
# §A-RECS-03 — _filter_unread: no state row → eligible (inbox assumed)
# Verified: recommender.py:220-245 (_filter_unread)
# Survivor-of: test_recommender.py::TestFilterUnread::test_filter_unread_includes_papers_with_no_state
# ---------------------------------------------------------------------------


async def test_filter_unread_includes_paper_with_no_state_row(contract_conn):
    """_filter_unread must return papers with NO paper_user_state row (assumed inbox)."""
    from paper_ingestion.ingestion.recommender import _filter_unread

    user_id = await contract_conn.fetchval(
        "INSERT INTO users (email, role) VALUES ('recs-nostate@contract.example.com', 'user') RETURNING id"
    )
    paper_id = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url)
           VALUES ('recs-nostate-01', 'arxiv', 'No State Paper', '{}', 'https://t.test/nostate')
           RETURNING id"""
    )
    # Deliberately no paper_user_state row

    result = await _filter_unread(contract_conn, [paper_id], user_id=user_id)
    assert paper_id in result, (
        "Paper with no paper_user_state row must remain eligible for recommendation "
        "(COALESCE(pus.state, 'inbox') is not in exclusion set)"
    )


# ---------------------------------------------------------------------------
# §A-RECS-04 — _filter_unread: negative feedback within 60 days → excluded
# Verified: recommender.py:220-245 (_filter_unread)
# Survivor-of: test_recommender.py::TestFilterUnread::test_filter_unread_excludes_negative_feedback_within_60_days
# ---------------------------------------------------------------------------


async def test_filter_unread_excludes_recent_negative_feedback(contract_conn):
    """_filter_unread must NOT return papers with negative feedback in the last 60 days."""
    from paper_ingestion.ingestion.recommender import _filter_unread

    user_id = await contract_conn.fetchval(
        "INSERT INTO users (email, role) VALUES ('recs-negfb@contract.example.com', 'user') RETURNING id"
    )
    paper_id = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url)
           VALUES ('recs-negfb-01', 'arxiv', 'Thumbs Down Paper', '{}', 'https://t.test/negfb')
           RETURNING id"""
    )
    # Insert a negative feedback row with created_at = NOW() (within 60 days)
    await contract_conn.execute(
        """INSERT INTO recommendation_feedback (paper_id, user_id, signal, source)
           VALUES ($1, $2, 'negative', 'pulse_thumbs')
           ON CONFLICT (paper_id, user_id, source) DO UPDATE SET signal = 'negative'""",
        paper_id,
        user_id,
    )

    result = await _filter_unread(contract_conn, [paper_id], user_id=user_id)
    assert paper_id not in result, (
        "Paper with recent (< 60 days) negative feedback must be excluded from _filter_unread "
        "(rf.signal = 'negative' AND rf.created_at > NOW() - INTERVAL '60 days')"
    )


# ---------------------------------------------------------------------------
# §A-RECS-05 — _filter_unread: negative feedback > 60 days ago → eligible
# Verified: recommender.py:220-245 (_filter_unread)
# Survivor-of: test_recommender.py::TestFilterUnread::test_filter_unread_includes_negative_feedback_older_than_60_days
# ---------------------------------------------------------------------------


async def test_filter_unread_includes_old_negative_feedback(contract_conn):
    """_filter_unread must return papers whose negative feedback is older than 60 days."""
    from paper_ingestion.ingestion.recommender import _filter_unread

    user_id = await contract_conn.fetchval(
        "INSERT INTO users (email, role) VALUES ('recs-oldfb@contract.example.com', 'user') RETURNING id"
    )
    paper_id = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url)
           VALUES ('recs-oldfb-01', 'arxiv', 'Old Disliked Paper', '{}', 'https://t.test/oldfb')
           RETURNING id"""
    )
    # Insert negative feedback with created_at explicitly set to 61 days ago
    await contract_conn.execute(
        """INSERT INTO recommendation_feedback (paper_id, user_id, signal, source, created_at)
           VALUES ($1, $2, 'negative', 'pulse_thumbs', NOW() - INTERVAL '61 days')
           ON CONFLICT (paper_id, user_id, source)
           DO UPDATE SET signal = 'negative', created_at = NOW() - INTERVAL '61 days'""",
        paper_id,
        user_id,
    )

    result = await _filter_unread(contract_conn, [paper_id], user_id=user_id)
    assert paper_id in result, (
        "Paper with negative feedback > 60 days old must be eligible again for recommendation "
        "(60-day window guard: rf.created_at > NOW() - INTERVAL '60 days' — old rows excluded from NOT EXISTS)"
    )


# ---------------------------------------------------------------------------
# §A-RECS-06 — _filter_unread: cross-user isolation (B cannot affect A's eligibility)
# Verified: recommender.py:220-245 (_filter_unread)
# ---------------------------------------------------------------------------


async def test_filter_unread_cross_user_isolation(contract_conn):
    """_filter_unread must not exclude a paper because another user trashed it.

    User B trashing a paper must not affect its eligibility for user A's
    recommendations. The filter is scoped by user_id = $2 in both NOT EXISTS subqueries.
    """
    from paper_ingestion.ingestion.recommender import _filter_unread

    user_a_id = await contract_conn.fetchval(
        "INSERT INTO users (email, role) VALUES ('recs-isoa@contract.example.com', 'user') RETURNING id"
    )
    user_b_id = await contract_conn.fetchval(
        "INSERT INTO users (email, role) VALUES ('recs-isob@contract.example.com', 'user') RETURNING id"
    )
    paper_id = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url)
           VALUES ('recs-iso-01', 'arxiv', 'Shared Paper ISO', '{}', 'https://t.test/iso')
           RETURNING id"""
    )
    # User B trashes the paper
    await contract_conn.execute(
        """INSERT INTO paper_user_state (paper_id, user_id, state)
           VALUES ($1, $2, 'trash')""",
        paper_id,
        user_b_id,
    )

    # User A has no state for this paper → should remain eligible for A
    result_a = await _filter_unread(contract_conn, [paper_id], user_id=user_a_id)
    assert paper_id in result_a, (
        "User B trashing a paper must not affect its eligibility for user A's recommendations "
        "(filter is WHERE pus.user_id = $2 — user-scoped isolation)"
    )


# ---------------------------------------------------------------------------
# §A-RECS-07 — GET /api/recommendations: dismissed=FALSE scope + user isolation
# Verified: recommendations.py:22-39 (list_recommendations)
# ---------------------------------------------------------------------------


async def test_list_recommendations_excludes_dismissed(
    contract_conn, contract_two_users, _pi_app, _configure_api_key
):
    """GET /api/recommendations excludes rows where dismissed=TRUE.

    The seeded paper_recommendations row (via contract_two_users) has dismissed=FALSE.
    We update it to dismissed=TRUE and assert it no longer appears in the response.
    """
    user_id = contract_two_users.user_a_id
    paper_id = contract_two_users.paper_id_a

    # Mark the seeded recommendation as dismissed
    await contract_conn.execute(
        "UPDATE paper_recommendations SET dismissed = TRUE WHERE paper_id = $1 AND user_id = $2",
        paper_id,
        user_id,
    )

    async with _client(_pi_app, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/recommendations")

    assert resp.status_code == 200
    body = resp.json()
    returned_ids = [r["paper_id"] for r in body]
    assert paper_id not in returned_ids, (
        "Dismissed recommendation must be excluded from GET /api/recommendations response"
    )


# ---------------------------------------------------------------------------
# §A-RECS-08 — GET /api/recommendations: user_id isolation (user B cannot see A's)
# Verified: recommendations.py:22-39 (list_recommendations)
# ---------------------------------------------------------------------------


async def test_list_recommendations_user_isolation(contract_two_users, _pi_app, _configure_api_key):
    """GET /api/recommendations returns only the requesting user's rows.

    User B's request must not see user A's recommendation row.
    """
    paper_id_a = contract_two_users.paper_id_a

    async with _client(_pi_app, contract_two_users.cookie_b) as c:
        resp = await c.get("/api/recommendations")

    assert resp.status_code == 200
    body = resp.json()
    returned_ids = [r["paper_id"] for r in body]
    assert paper_id_a not in returned_ids, (
        "User B must not see user A's recommendation row — user_id isolation required"
    )


# ---------------------------------------------------------------------------
# §A-RECS-09 — POST /api/recommendations/{paper_id}/dismiss: DB mutation
# Verified: recommendations.py:51-68 (dismiss_recommendation)
# ---------------------------------------------------------------------------


async def test_dismiss_recommendation_sets_dismissed_flag(
    contract_conn, contract_two_users, _pi_app, _configure_api_key
):
    """POST /api/recommendations/{id}/dismiss marks dismissed=TRUE in DB.

    Asserts the DB row is updated — strictly stronger than a mock-unit test
    that only checks the SQL string.
    """
    user_id = contract_two_users.user_a_id
    paper_id = contract_two_users.paper_id_a

    async with _client(_pi_app, contract_two_users.cookie_a) as c:
        resp = await c.post(f"/api/recommendations/{paper_id}/dismiss")

    assert resp.status_code == 200, (
        f"Expected 200 from dismiss; got {resp.status_code}: {resp.text}"
    )
    body = resp.json()
    assert body.get("dismissed") is True

    # Verify DB state directly
    row = await contract_conn.fetchrow(
        "SELECT dismissed FROM paper_recommendations WHERE paper_id = $1 AND user_id = $2",
        paper_id,
        user_id,
    )
    assert row is not None, "Recommendation row must still exist after dismiss"
    assert row["dismissed"] is True, "DB row must have dismissed=TRUE after POST /dismiss"


# ---------------------------------------------------------------------------
# §A-RECS-10 — POST /api/recommendations/{paper_id}/dismiss: 404 for non-existent
# Verified: recommendations.py:60-67 (dismiss_recommendation 404 path)
# ---------------------------------------------------------------------------


async def test_dismiss_recommendation_404_for_nonexistent(
    contract_two_users, _pi_app, _configure_api_key
):
    """POST /api/recommendations/{id}/dismiss returns 404 when no row exists."""
    async with _client(_pi_app, contract_two_users.cookie_a) as c:
        resp = await c.post("/api/recommendations/999999999/dismiss")

    assert resp.status_code == 404, (
        f"Expected 404 for non-existent recommendation; got {resp.status_code}"
    )


# ---------------------------------------------------------------------------
# E1.PI extensions — owner-scope isolation, starred eligibility
#
# Verified: recommender.py:220-245 (_filter_unread SQL predicates)
# Verified: recommendations.py:22-39 (list_recommendations WHERE user_id = $1)
# ---------------------------------------------------------------------------


async def test_filter_unread_starred_paper_remains_eligible(contract_conn):
    """_filter_unread must return papers whose starred flag is TRUE.

    The exclusion predicate is: COALESCE(pus.state, 'inbox') IN ('trash', 'done').
    The starred boolean is NOT an exclusion predicate — starred papers remain recommendable.
    Verified: recommender.py:220-245 (_filter_unread exclusion set).
    Survivor-of (Phase E2): test_recommender.py::TestFilterUnread::test_starred_papers_remain_eligible_for_recommendation.
    """
    from paper_ingestion.ingestion.recommender import _filter_unread

    user_id = await contract_conn.fetchval(
        "INSERT INTO users (email, role) VALUES ('recs-starred@contract.example.com', 'user') RETURNING id"
    )
    paper_id = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url)
           VALUES ('recs-starred-01', 'arxiv', 'Starred Paper', '{}', 'https://t.test/starred')
           RETURNING id"""
    )
    await contract_conn.execute(
        """INSERT INTO paper_user_state (paper_id, user_id, state, starred)
           VALUES ($1, $2, 'to_read', TRUE)
           ON CONFLICT (paper_id, user_id)
           DO UPDATE SET state = 'to_read', starred = TRUE""",
        paper_id,
        user_id,
    )

    result = await _filter_unread(contract_conn, [paper_id], user_id=user_id)
    assert paper_id in result, (
        "Paper with starred=TRUE must remain eligible for recommendations "
        "(starred is not in the COALESCE(...) IN ('trash', 'done') exclusion set)"
    )


async def test_list_recommendations_owner_scope_excludes_other_user_rows(
    contract_conn, contract_two_users, _pi_app, _configure_api_key
):
    """GET /api/recommendations only returns the caller's own rows (strict user_id scope).

    Seeds a fresh recommendation row for user B and verifies user A cannot see it.
    Verified: recommendations.py:22-39 (list_recommendations WHERE pr.user_id = $1).
    Survivor-of (Phase E2): test_recommender.py owner-scope isolation tests.
    """
    user_b_id = contract_two_users.user_b_id

    # Seed a paper and recommendation row owned only by user B
    b_paper_id = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by)
           VALUES ('recs-scope-b-01', 'arxiv', 'Scope B Paper', ARRAY['Au'],
                   'https://scope-b.test/1', $1)
           RETURNING id""",
        user_b_id,
    )
    await contract_conn.execute(
        """INSERT INTO paper_recommendations (paper_id, user_id, score, dismissed)
           VALUES ($1, $2, 0.9, FALSE)
           ON CONFLICT (paper_id, user_id) DO UPDATE SET score = 0.9""",
        b_paper_id,
        user_b_id,
    )

    # User A's request must NOT include user B's recommendation row
    async with _client(_pi_app, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/recommendations")

    assert resp.status_code == 200
    returned_ids = [r["paper_id"] for r in resp.json()]
    assert b_paper_id not in returned_ids, (
        f"User A must not see user B's recommendation row (paper_id={b_paper_id}); "
        f"got returned_ids={returned_ids}"
    )


async def test_filter_unread_empty_candidate_list_returns_empty(contract_conn):
    """_filter_unread with an empty candidate list returns an empty set immediately.

    Guards against SQL errors from empty ANY($1::int[]) bindings.
    Verified: recommender.py:220-245 (_filter_unread edge-case guard).
    """
    from paper_ingestion.ingestion.recommender import _filter_unread

    user_id = await contract_conn.fetchval(
        "INSERT INTO users (email, role) VALUES ('recs-empty@contract.example.com', 'user') RETURNING id"
    )
    result = await _filter_unread(contract_conn, [], user_id=user_id)
    assert result == set() or len(result) == 0, (
        "Empty candidate list must return empty set from _filter_unread"
    )


# ---------------------------------------------------------------------------
# Cluster 9 — Recommender weights precedence (C9-03)
# Survivor-of test_recommender.py::TestReadWeightsPrecedence::* (4 mock-units).
# C9-01/C9-02/C9-04 require full _refresh_recommendations_for_user with embedder
# carve-out wiring — deferred to rot-on-touch (existing TestComputeScore +
# TestAggregateToPapers pure-unit tests cover the scoring math).
# ---------------------------------------------------------------------------


async def test_c9_03_read_weights_user_row_wins_over_global(contract_two_users, contract_conn):
    """_read_weights: per-user row precedence > global (user_id IS NULL) row.

    # Verified: services/paper_ingestion/paper_ingestion/ingestion/recommender.py:155
    # (_read_weights ORDER BY key, user_id NULLS LAST; user-row sets _cfg_raw[k]
    # only if non-NULL, overriding any prior global row).
    """
    from paper_ingestion.ingestion.recommender import _read_weights

    # Global row (user_id NULL): liked_weight=0.3
    await contract_conn.execute(
        """
        INSERT INTO user_config (key, value, user_id)
        VALUES ('recommendation.liked_weight', $1::jsonb, NULL)
        ON CONFLICT (key, user_id) DO UPDATE SET value = EXCLUDED.value
        """,
        "0.3",
    )
    # Per-user row for user A: liked_weight=0.8
    await contract_conn.execute(
        """
        INSERT INTO user_config (key, value, user_id)
        VALUES ('recommendation.liked_weight', $1::jsonb, $2)
        ON CONFLICT (key, user_id) DO UPDATE SET value = EXCLUDED.value
        """,
        "0.8",
        contract_two_users.user_a_id,
    )

    liked_a, _, _ = await _read_weights(contract_conn, contract_two_users.user_a_id)
    assert liked_a == 0.8, (
        f"User A's per-user liked_weight=0.8 should win over global 0.3; got {liked_a}"
    )

    # User B with no per-user row should see the global 0.3
    liked_b, _, _ = await _read_weights(contract_conn, contract_two_users.user_b_id)
    assert liked_b == 0.3, (
        f"User B with no per-user row should see global liked_weight=0.3; got {liked_b}"
    )


# ---------------------------------------------------------------------------
# §W1A.4-RECS-01 — POST /api/recommendations/refresh persists scores user-scoped
#
# Verified: services/paper_ingestion/paper_ingestion/routers/recommendations.py:42-48
#   (trigger_refresh: calls refresh_recommendations(app, user_id=user_id))
# Verified: services/paper_ingestion/paper_ingestion/ingestion/recommender.py:132-144
#   (upsert into paper_recommendations with user_id column)
# Survivor-of: ~3-5 mock-units in test_recommender.py that stub the upsert SQL
# ---------------------------------------------------------------------------


async def test_recommendations_refresh_endpoint_persists_scores_user_scoped(
    contract_conn, contract_two_users, _pi_app, _configure_api_key, monkeypatch
):
    """POST /api/recommendations/refresh persists recommendation rows for the
    calling user and does NOT write rows for other users.

    The embedder.discover_from_seeds and embedder.search_similar paths are
    the §5.1 carve-out boundary (Qdrant/embed). We inject one seeded
    starred paper, stub the embedder to return one score for a candidate
    paper, and assert the DB row is written with the correct user_id.
    # Verified: recommendations.py:42-48 (trigger_refresh)
    # Verified: recommender.py:71-88 (embedder calls + score merge)
    # Verified: recommender.py:132-144 (paper_recommendations UPSERT)
    """
    from unittest.mock import AsyncMock, MagicMock

    user_a_id = contract_two_users.user_a_id
    user_b_id = contract_two_users.user_b_id

    # Seed two candidate papers
    paper_candidate_id = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url)
           VALUES ('refresh-cand-01', 'arxiv', 'Candidate Paper', '{}', 'https://r.test/1')
           RETURNING id"""
    )
    await contract_conn.execute(
        """INSERT INTO papers (external_id, source_type, title, authors, url)
           VALUES ('refresh-cand-02', 'arxiv', 'Other User Paper', '{}', 'https://r.test/2')""",
    )

    # Seed starred paper for user A
    starred_paper_id = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url)
           VALUES ('refresh-starred-01', 'arxiv', 'Starred Paper', '{}', 'https://r.test/s')
           RETURNING id"""
    )
    await contract_conn.execute(
        """INSERT INTO paper_user_state (paper_id, user_id, state, starred)
           VALUES ($1, $2, 'to_read', TRUE)
           ON CONFLICT (paper_id, user_id) DO UPDATE SET starred = TRUE""",
        starred_paper_id,
        user_a_id,
    )

    # Stub embedder: discover_from_seeds returns score for candidate paper
    stub_embedder = MagicMock()
    stub_embedder.discover_from_seeds = AsyncMock(
        return_value=[{"paper_id": paper_candidate_id, "score": 0.85}]
    )
    stub_embedder.search_similar = AsyncMock(return_value=[])
    _pi_app.state.embedder = stub_embedder

    async with _client(_pi_app, contract_two_users.cookie_a) as c:
        resp = await c.post("/api/recommendations/refresh")

    assert resp.status_code == 200, (
        f"Expected 200 from refresh; got {resp.status_code}: {resp.text}"
    )
    body = resp.json()
    assert body.get("refreshed", 0) >= 1, f"Expected at least 1 refreshed; got {body}"

    # DB must have a row for user A with the candidate paper
    row = await contract_conn.fetchrow(
        "SELECT score, user_id FROM paper_recommendations WHERE paper_id = $1 AND user_id = $2",
        paper_candidate_id,
        user_a_id,
    )
    assert row is not None, "paper_recommendations must have a row for user_a after refresh"
    assert row["score"] >= 0.25, f"Score must be >= _MIN_SCORE=0.25; got {row['score']}"
    assert row["user_id"] == user_a_id, "Row must be scoped to user A"

    # User B must have NO row for this paper
    other = await contract_conn.fetchrow(
        "SELECT 1 FROM paper_recommendations WHERE paper_id = $1 AND user_id = $2",
        paper_candidate_id,
        user_b_id,
    )
    assert other is None, "User B must not have a recommendation row seeded by user A's refresh"
