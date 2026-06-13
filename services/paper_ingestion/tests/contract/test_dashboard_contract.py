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
