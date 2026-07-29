"""Tests for C2 user_id threading in submit_feedback.

C2: submit_feedback INSERT must include user_id; conflict key must include user_id.

Note: C1 (mark_paper_read) tests were removed in Phase-A lifecycle redesign because
the mark_paper_read endpoint was deleted (replaced by annotate_paper / state machine).

``submit_feedback`` delegates the write to
``_upsert_recommendation_feedback`` which:
  * issues ``conn.fetchval(...)`` to look up the paper's primary topic_id
    (when not supplied) — 1 fetchval call always
  * issues ``conn.execute(...)`` for the INSERT...ON CONFLICT — 1 execute call,
    no RETURNING (the endpoint synthesises its own ``FeedbackResponse`` from
    the request body + ``datetime.now(UTC)``)
"""

from __future__ import annotations

from unittest.mock import MagicMock

from paper_ingestion.models import FeedbackRequest
from paper_ingestion.routers import papers

from tests.conftest import _make_pool_and_conn

# ---------------------------------------------------------------------------
# C2: submit_feedback — user_id must be in INSERT and SELECT
# ---------------------------------------------------------------------------


async def test_submit_feedback_threads_user_id_to_select():
    """C2: submit_feedback INSERT uses ON CONFLICT keyed on (paper_id, user_id, source).

    Verifies that user_id appears in the conflict target SQL so repeat submissions
    by different users never overwrite each other.  Cross-user isolation: a real
    user_id is bound at $2 (no NULL-shared rows).
    Group B: source='pulse_thumbs' is pulse-only, so fetchrow validates discovery_origin.
    """
    pool, conn = _make_pool_and_conn()
    # Group B: source='pulse_thumbs' is pulse-only; fetchrow validates discovery_origin.
    conn.fetchrow.return_value = {
        "id": 10,
        "is_visible": True,
        "discovery_origin": "pulse_discovery",
    }
    conn.fetchval.return_value = None  # no topic mapping

    # CC-03: explicit caller identity for the direct (non-ASGI) handler call;
    # value matches the pre-conversion autouse symbol stub (user 1).
    await papers.submit_feedback.__wrapped__(
        MagicMock(),
        paper_id=10,
        body=FeedbackRequest(signal="negative", source="pulse_thumbs"),
        db_pool=pool,
        user_id=1,
    )

    assert conn.execute.await_count == 1
    execute_args = conn.execute.await_args.args
    insert_sql = execute_args[0]

    # The INSERT must use ON CONFLICT keyed on user_id so different users
    # can each store their own signal for the same paper.
    assert "ON CONFLICT" in insert_sql, f"Expected 'ON CONFLICT' in INSERT SQL, got:\n{insert_sql}"
    assert "user_id" in insert_sql

    # Positional bind $2 must be the real authenticated user_id
    positional_binds = execute_args[1:]
    assert positional_binds[1] == 1  # user_id at $2 (real user)
