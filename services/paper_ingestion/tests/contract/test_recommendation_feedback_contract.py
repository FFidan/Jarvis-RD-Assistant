"""Contract tests for /api/recommendation_feedback (A110, A111).

Routes under test:
  - GET  /api/recommendation_feedback       (list_recommendation_feedback)
  - DELETE /api/recommendation_feedback?topic_id=N (delete_recommendation_feedback_by_topic)

Verified:
  - services/paper_ingestion/paper_ingestion/routers/recommendation_feedback.py:38
    (list_recommendation_feedback: user-scoped SELECT + optional paper_id filter)
  - services/paper_ingestion/paper_ingestion/routers/recommendation_feedback.py:90
    (delete_recommendation_feedback_by_topic: user-scoped DELETE by topic_id)

Response models (verified from paper_ingestion/models/papers.py):
  - FeedbackListResponse: {items: list[FeedbackListItem], total: int}
  - FeedbackListItem: {paper_id, title, signal, source, reason, topic_id, topic_name, created_at}
  - DeleteFeedbackResponse: {deleted: int, topic_id: int}

Unique constraint on recommendation_feedback:
  recommendation_feedback_paper_user_source_uniq (paper_id, user_id, source) — NULLS NOT DISTINCT
"""

from __future__ import annotations

import pytest
from jarvis_common.testing_contract_apps import make_contract_client as _client

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]


# ---------------------------------------------------------------------------
# §A110-01 — GET /api/recommendation_feedback: user_id scoping
# Verified: recommendation_feedback.py:53 (WHERE rf.user_id = $1)
# ---------------------------------------------------------------------------


async def test_get_recommendation_feedback_owner_sees_own_rows(
    contract_conn, contract_two_users, _pi_app, _configure_api_key
):
    """A110: GET returns rows scoped to the caller's user_id; other user's rows absent.

    Seeds 2 feedback rows for user A (different papers) and 1 for user B on a
    separate paper. Asserts user A's GET returns exactly 2 items and user B's
    GET returns exactly 1 item, with no cross-user leakage.

    Verified: recommendation_feedback.py:53 (WHERE rf.user_id = $1).
    """
    user_a_id = contract_two_users.user_a_id
    user_b_id = contract_two_users.user_b_id

    paper_a1 = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url)
           VALUES ('rfb-a110-a1', 'arxiv', 'Feedback Paper A1', '{}', 'https://rfb.test/a1')
           RETURNING id"""
    )
    paper_a2 = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url)
           VALUES ('rfb-a110-a2', 'arxiv', 'Feedback Paper A2', '{}', 'https://rfb.test/a2')
           RETURNING id"""
    )
    paper_b1 = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url)
           VALUES ('rfb-a110-b1', 'arxiv', 'Feedback Paper B1', '{}', 'https://rfb.test/b1')
           RETURNING id"""
    )

    # 2 rows for user A
    await contract_conn.executemany(
        """INSERT INTO recommendation_feedback (paper_id, user_id, signal, source)
           VALUES ($1, $2, 'negative', 'pulse_thumbs')""",
        [(paper_a1, user_a_id), (paper_a2, user_a_id)],
    )
    # 1 row for user B
    await contract_conn.execute(
        """INSERT INTO recommendation_feedback (paper_id, user_id, signal, source)
           VALUES ($1, $2, 'positive', 'feed_thumbs')""",
        paper_b1,
        user_b_id,
    )

    async with _client(_pi_app, contract_two_users.cookie_a) as c:
        resp_a = await c.get("/api/recommendation_feedback")

    assert resp_a.status_code == 200, f"User A GET failed: {resp_a.status_code} {resp_a.text}"
    body_a = resp_a.json()
    a_paper_ids = [item["paper_id"] for item in body_a["items"]]
    assert paper_a1 in a_paper_ids and paper_a2 in a_paper_ids, (
        f"User A must see both own feedback rows; got paper_ids={a_paper_ids}"
    )
    assert paper_b1 not in a_paper_ids, (
        "User A must NOT see user B's feedback row (user_id isolation)"
    )

    async with _client(_pi_app, contract_two_users.cookie_b) as c:
        resp_b = await c.get("/api/recommendation_feedback")

    assert resp_b.status_code == 200, f"User B GET failed: {resp_b.status_code} {resp_b.text}"
    body_b = resp_b.json()
    b_paper_ids = [item["paper_id"] for item in body_b["items"]]
    assert paper_b1 in b_paper_ids, "User B must see their own feedback row"
    assert paper_a1 not in b_paper_ids and paper_a2 not in b_paper_ids, (
        "User B must NOT see user A's feedback rows (user_id isolation)"
    )


# ---------------------------------------------------------------------------
# §A110-02 — GET /api/recommendation_feedback: ?paper_id filter
# Verified: recommendation_feedback.py:56-57 (rf.paper_id = $2 clause)
# ---------------------------------------------------------------------------


async def test_get_recommendation_feedback_paper_id_filter(
    contract_conn, contract_two_users, _pi_app, _configure_api_key
):
    """A110: ?paper_id=N narrows the GET response to that paper only.

    Seeds 2 feedback rows for user A on different papers; the ?paper_id filter
    must return exactly 1 row matching the requested paper_id.

    Verified: recommendation_feedback.py:56-57 (WHERE rf.paper_id = $2 clause).
    """
    user_a_id = contract_two_users.user_a_id

    paper_x = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url)
           VALUES ('rfb-a110-px', 'arxiv', 'Filter Paper X', '{}', 'https://rfb.test/px')
           RETURNING id"""
    )
    paper_y = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url)
           VALUES ('rfb-a110-py', 'arxiv', 'Filter Paper Y', '{}', 'https://rfb.test/py')
           RETURNING id"""
    )

    await contract_conn.executemany(
        """INSERT INTO recommendation_feedback (paper_id, user_id, signal, source)
           VALUES ($1, $2, 'negative', 'pulse_thumbs')""",
        [(paper_x, user_a_id), (paper_y, user_a_id)],
    )

    async with _client(_pi_app, contract_two_users.cookie_a) as c:
        resp = await c.get(f"/api/recommendation_feedback?paper_id={paper_x}")

    assert resp.status_code == 200, f"GET with paper_id filter failed: {resp.status_code}"
    body = resp.json()
    returned_paper_ids = [item["paper_id"] for item in body["items"]]
    assert paper_x in returned_paper_ids, (
        f"?paper_id={paper_x} must include that paper's row; got {returned_paper_ids}"
    )
    assert paper_y not in returned_paper_ids, (
        f"?paper_id={paper_x} must exclude paper_y={paper_y}; got {returned_paper_ids}"
    )
    assert body["total"] == 1, f"total must be 1 for single paper_id filter; got {body['total']}"


# ---------------------------------------------------------------------------
# §A110-03 — GET /api/recommendation_feedback: 401 without session
# Verified: auth.py:468 (get_current_user_id → current_user_id_strict_with_owner_override)
# ---------------------------------------------------------------------------


async def test_get_recommendation_feedback_requires_auth(_pi_app, _configure_api_key):
    """A110: GET without a valid session cookie returns 401.

    The route uses Depends(get_current_user_id) which resolves through
    current_user_id_strict_with_owner_override — no cookie → 401.
    """
    async with _client(_pi_app, None) as c:
        resp = await c.get("/api/recommendation_feedback")

    assert resp.status_code == 401, (
        f"Unauthenticated GET /api/recommendation_feedback must return 401; got {resp.status_code}"
    )


# ---------------------------------------------------------------------------
# §A111-01 — DELETE /api/recommendation_feedback: owner-only deletion
# Verified: recommendation_feedback.py:103-109 (WHERE topic_id = $1 AND user_id = $2)
# ---------------------------------------------------------------------------


async def test_delete_recommendation_feedback_owner_deletes_own_only(
    contract_conn, contract_two_users, _pi_app, _configure_api_key
):
    """A111: DELETE removes caller's rows on the topic_id; other user's same-topic rows intact.

    Seeds feedback rows for user A and user B against the same topic_id.
    After user A deletes, asserts:
      - DeleteFeedbackResponse.deleted == number of A's rows on that topic
      - User B's feedback rows for that topic still exist in the DB

    Verified: recommendation_feedback.py:103-109 (WHERE topic_id=$1 AND user_id=$2).
    """
    user_a_id = contract_two_users.user_a_id
    user_b_id = contract_two_users.user_b_id

    topic_id = await contract_conn.fetchval(
        """INSERT INTO topics (name, query_terms)
           VALUES ('rfb-delete-topic', ARRAY['rfb-test'])
           RETURNING id"""
    )

    paper_da1 = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url)
           VALUES ('rfb-a111-da1', 'arxiv', 'Delete Paper A1', '{}', 'https://rfb.test/da1')
           RETURNING id"""
    )
    paper_da2 = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url)
           VALUES ('rfb-a111-da2', 'arxiv', 'Delete Paper A2', '{}', 'https://rfb.test/da2')
           RETURNING id"""
    )
    paper_db1 = await contract_conn.fetchval(
        """INSERT INTO papers (external_id, source_type, title, authors, url)
           VALUES ('rfb-a111-db1', 'arxiv', 'Delete Paper B1', '{}', 'https://rfb.test/db1')
           RETURNING id"""
    )

    # 2 rows for user A tied to the topic
    await contract_conn.executemany(
        """INSERT INTO recommendation_feedback (paper_id, user_id, signal, source, topic_id)
           VALUES ($1, $2, 'negative', 'pulse_thumbs', $3)""",
        [(paper_da1, user_a_id, topic_id), (paper_da2, user_a_id, topic_id)],
    )
    # 1 row for user B on the SAME topic
    await contract_conn.execute(
        """INSERT INTO recommendation_feedback (paper_id, user_id, signal, source, topic_id)
           VALUES ($1, $2, 'negative', 'feed_thumbs', $3)""",
        paper_db1,
        user_b_id,
        topic_id,
    )

    async with _client(_pi_app, contract_two_users.cookie_a) as c:
        resp = await c.delete(f"/api/recommendation_feedback?topic_id={topic_id}")

    assert resp.status_code == 200, f"DELETE failed: {resp.status_code} {resp.text}"
    body = resp.json()
    assert body["deleted"] == 2, f"Expected deleted=2 (user A's 2 rows); got {body['deleted']}"
    assert body["topic_id"] == topic_id, (
        f"Response topic_id must echo {topic_id}; got {body['topic_id']}"
    )

    # User B's row must still be present
    remaining_b = await contract_conn.fetchval(
        """SELECT COUNT(*) FROM recommendation_feedback
           WHERE user_id = $1 AND topic_id = $2""",
        user_b_id,
        topic_id,
    )
    assert remaining_b == 1, (
        f"User B's feedback row on topic_id={topic_id} must survive A's DELETE; "
        f"found {remaining_b} rows"
    )

    # User A's rows must be gone
    remaining_a = await contract_conn.fetchval(
        """SELECT COUNT(*) FROM recommendation_feedback
           WHERE user_id = $1 AND topic_id = $2""",
        user_a_id,
        topic_id,
    )
    assert remaining_a == 0, (
        f"User A's feedback rows must be deleted; found {remaining_a} rows remaining"
    )


# ---------------------------------------------------------------------------
# §A111-02 — DELETE /api/recommendation_feedback: 401 without session
# Verified: auth.py:468 (get_current_user_id → current_user_id_strict_with_owner_override)
# ---------------------------------------------------------------------------


async def test_delete_recommendation_feedback_requires_auth(_pi_app, _configure_api_key):
    """A111: DELETE without a valid session cookie returns 401.

    The route uses Depends(get_current_user_id) — no cookie → 401 before any
    SQL executes.
    """
    async with _client(_pi_app, None) as c:
        resp = await c.delete("/api/recommendation_feedback?topic_id=1")

    assert resp.status_code == 401, (
        f"Unauthenticated DELETE /api/recommendation_feedback must return 401; got {resp.status_code}"
    )
