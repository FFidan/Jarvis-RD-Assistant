"""Dashboard domain contract tests — target row A32.

Survivor-of: test_dashboard_api.py mock-unit assertions for get_dashboard_metrics.
Carve-out: app.state.http_client is MagicMock (outbound HTTP).
"""

from __future__ import annotations

import pytest

from jarvis_common.testing_contract_apps import (
    make_contract_client as _make_client,
)

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]


# ---------------------------------------------------------------------------
# A32: GET /api/dashboard/metrics — aggregate counts scoped to current user
# ---------------------------------------------------------------------------


async def test_a32_dashboard_metrics_returns_all_fields(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
):
    """Covers map row A32: GET /api/dashboard/metrics returns all required aggregate fields."""
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/dashboard/metrics")

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text[:300]}"
    body = resp.json()
    for field in (
        "total_papers",
        "unread_papers",
        "pending_papers",
        "due_cards",
        "active_projects",
        "topic_count",
        "nudge_count",
        "chunked_papers",
    ):
        assert field in body, f"Missing field {field!r} in dashboard metrics: {body}"
    # All counts must be non-negative integers
    for field in ("total_papers", "unread_papers", "pending_papers", "due_cards", "chunked_papers"):
        assert isinstance(body[field], int) and body[field] >= 0, (
            f"Field {field!r} must be non-negative int; got {body[field]!r}"
        )


async def test_a32_dashboard_metrics_total_papers_matches_user_library(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """Covers map row A32: total_papers equals actual user_library row count for user A.

    Verified: dashboard_api.py:46-49 COUNT(*) FROM user_library WHERE user_id=$1.
    """
    # Count user A's library rows directly
    db_count = await contract_conn.fetchval(
        "SELECT COUNT(*) FROM user_library WHERE user_id = $1",
        contract_two_users.user_a_id,
    )

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/dashboard/metrics")

    assert resp.status_code == 200
    body = resp.json()
    assert body["total_papers"] == db_count, (
        f"total_papers={body['total_papers']} != user_library count={db_count} for user A"
    )


async def test_a32_dashboard_metrics_user_b_gets_own_counts(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """Covers map row A32: user B's metrics are scoped to user B's library (not user A's).

    Verified: dashboard_api.py:44 WHERE user_id=$1 user-scoped queries.
    """
    db_count_a = await contract_conn.fetchval(
        "SELECT COUNT(*) FROM user_library WHERE user_id = $1",
        contract_two_users.user_a_id,
    )
    db_count_b = await contract_conn.fetchval(
        "SELECT COUNT(*) FROM user_library WHERE user_id = $1",
        contract_two_users.user_b_id,
    )

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_b) as c:
        resp = await c.get("/api/dashboard/metrics")

    assert resp.status_code == 200
    body = resp.json()
    assert body["total_papers"] == db_count_b, (
        f"User B's total_papers={body['total_papers']} != {db_count_b}"
    )
    # If users have different counts, this proves scoping
    if db_count_a != db_count_b:
        assert body["total_papers"] != db_count_a, (
            f"User B is leaking user A's library count ({db_count_a}) — IDOR"
        )


# ---------------------------------------------------------------------------
# chunked_papers — Ask gating
# ---------------------------------------------------------------------------


async def test_chunked_papers_zero_when_no_chunks(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """chunked_papers is 0 when the user's papers have no paper_chunks rows."""
    db_chunks = await contract_conn.fetchval(
        """SELECT COUNT(DISTINCT ul.paper_id) FROM user_library ul
           WHERE ul.user_id = $1
             AND EXISTS (SELECT 1 FROM paper_chunks pc WHERE pc.paper_id = ul.paper_id)""",
        contract_two_users.user_a_id,
    )
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/dashboard/metrics")

    assert resp.status_code == 200
    body = resp.json()
    assert body["chunked_papers"] == int(db_chunks), (
        f"chunked_papers={body['chunked_papers']} != db count={db_chunks}"
    )


async def test_chunked_papers_reflects_chunk_presence(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """chunked_papers increases to 1 once a paper_chunks row exists for the user's paper.

    Verifies the Ask-gate premise: a user with an analyzed paper (chunk present) but
    zero topics still gets chunked_papers > 0, enabling the Ask UI without any topics.
    """
    paper_id = contract_two_users.paper_id_a

    await contract_conn.execute(
        """INSERT INTO paper_chunks (paper_id, chunk_index, content)
           VALUES ($1, 0, 'sample chunk for Ask-gate test')
           ON CONFLICT (paper_id, chunk_index) DO NOTHING""",
        paper_id,
    )
    try:
        async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
            resp = await c.get("/api/dashboard/metrics")

        assert resp.status_code == 200
        body = resp.json()
        assert body["chunked_papers"] >= 1, (
            f"Expected chunked_papers >= 1 after inserting chunk for paper {paper_id}; got {body['chunked_papers']}"
        )
    finally:
        await contract_conn.execute(
            "DELETE FROM paper_chunks WHERE paper_id = $1 AND chunk_index = 0",
            paper_id,
        )


async def test_chunked_papers_is_user_scoped(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """A chunk on user A's paper must not inflate user B's chunked_papers.

    Pins tenant isolation for the Ask-gate metric: the per-user query is
    ``COUNT(DISTINCT ul.paper_id) ... WHERE ul.user_id = $1`` — so a chunk on a
    paper outside B's library can never raise B's count.
    """
    paper_id = contract_two_users.paper_id_a

    await contract_conn.execute(
        """INSERT INTO paper_chunks (paper_id, chunk_index, content)
           VALUES ($1, 0, 'isolation chunk for user A')
           ON CONFLICT (paper_id, chunk_index) DO NOTHING""",
        paper_id,
    )
    try:
        expected_b = await contract_conn.fetchval(
            """SELECT COUNT(DISTINCT ul.paper_id) FROM user_library ul
               WHERE ul.user_id = $1
                 AND EXISTS (SELECT 1 FROM paper_chunks pc WHERE pc.paper_id = ul.paper_id)""",
            contract_two_users.user_b_id,
        )
        async with _make_client(_pi_app_with_pool, contract_two_users.cookie_b) as c:
            resp = await c.get("/api/dashboard/metrics")

        assert resp.status_code == 200
        body = resp.json()
        assert body["chunked_papers"] == int(expected_b), (
            f"User B's chunked_papers={body['chunked_papers']} != scoped db count {expected_b} — "
            "a chunk on user A's paper leaked into B's Ask-gate metric"
        )
    finally:
        await contract_conn.execute(
            "DELETE FROM paper_chunks WHERE paper_id = $1 AND chunk_index = 0",
            paper_id,
        )


async def test_pending_papers_scoped_to_calling_user(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """User A's summary on a shared paper must not lower user B's pending_papers count.

    Seed a shared paper in both libraries; only user A has a summary. User B's
    paper is unsummarized, so it must still count as pending for B.
    """
    paper_id = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url, discovered_by)
           VALUES ('dash-iso-shared', 'arxiv', 'shared-paper-dashboard-isolation',
                   ARRAY['A. Author'], 'https://example.test/dash-iso', NULL)
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
           VALUES ($1, 'A summary', 'A summary', $2)""",
        paper_id,
        contract_two_users.user_a_id,
    )

    # Ground truth: B's own pending count (papers in B's library with no summary owned by B).
    expected_b = await contract_conn.fetchval(
        """SELECT COUNT(*) FROM user_library ul
           LEFT JOIN paper_summaries ps
             ON ul.paper_id = ps.paper_id AND ps.user_id = $1
           WHERE ul.user_id = $1 AND ps.id IS NULL""",
        contract_two_users.user_b_id,
    )

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_b) as c:
        resp = await c.get("/api/dashboard/metrics")

    assert resp.status_code == 200, f"{resp.status_code}: {resp.text[:300]}"
    body = resp.json()
    assert body["pending_papers"] == int(expected_b), (
        f"pending_papers={body['pending_papers']} != scoped {expected_b}; "
        "user A's summary leaked into B's pending count"
    )


async def test_dashboard_pending_and_due_counts_follow_current_generation(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """Replacement makes old summaries pending again and removes stale cards from due."""
    paper_id = contract_two_users.paper_id_a
    card_id = contract_two_users.card_id_a
    user_id = contract_two_users.user_a_id
    await contract_conn.execute(
        """
        INSERT INTO paper_summaries
            (paper_id, summary_brief, summary_detailed, user_id, content_generation)
        SELECT p.id, 'current', 'current', $2, p.content_generation
        FROM papers p
        WHERE p.id = $1
        """,
        paper_id,
        user_id,
    )
    await contract_conn.execute(
        """
        UPDATE cards
        SET due_at = NOW() - INTERVAL '1 hour',
            content_generation = (
                SELECT content_generation FROM papers WHERE id = $2
            )
        WHERE id = $1
        """,
        card_id,
        paper_id,
    )

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        current = await c.get("/api/dashboard/metrics")
    assert current.status_code == 200, current.text[:300]
    assert current.json()["pending_papers"] == 0
    assert current.json()["due_cards"] == 1

    await contract_conn.execute(
        "UPDATE papers SET content_generation = content_generation + 1 WHERE id = $1",
        paper_id,
    )
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        replaced = await c.get("/api/dashboard/metrics")
    assert replaced.status_code == 200, replaced.text[:300]
    assert replaced.json()["pending_papers"] == 1
    assert replaced.json()["due_cards"] == 0
