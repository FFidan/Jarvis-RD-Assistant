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


async def test_submit_feedback_threads_user_id_to_insert():
    """C2: submit_feedback INSERT must include user_id as $2 (signal/source/reason after it).

    Writes to recommendation_feedback with binds ($1=paper_id, $2=user_id,
    $3=signal, $4=source, $5=reason, $6=topic_id). Cross-user isolation: the
    resolver yields a real user, so ``assert_paper_ownership`` runs (1
    fetchrow, persisted-visibility grant) in addition to the Group B
    discovery_origin fetchrow.
    """
    pool, conn = _make_pool_and_conn()
    # Group B: source='feed_thumbs' is pulse-only; fetchrow validates discovery_origin.
    conn.fetchrow.return_value = {
        "id": 7,
        "is_visible": True,
        "discovery_origin": "pulse_discovery",
    }
    # fetchval returns topic_id from paper_topics lookup; None means no topic
    conn.fetchval.return_value = None

    # CC-03: the handler now declares ``user_id: int = Depends(get_current_user_id)``;
    # a direct ``.__wrapped__`` call bypasses FastAPI injection, so the caller
    # identity is passed explicitly (pre-conversion the autouse symbol stub
    # supplied user 1 to the in-body resolver call — identical value here).
    result = await papers.submit_feedback.__wrapped__(
        MagicMock(),
        paper_id=7,
        body=FeedbackRequest(signal="positive", source="feed_thumbs"),
        db_pool=pool,
        user_id=1,
    )

    assert result.paper_id == 7
    assert result.signal == "positive"

    # Cross-user isolation: the persisted-visibility check and the feedback
    # discovery-origin validation each issue one fetchrow.
    assert conn.fetchrow.await_count == 2
    # INSERT goes through conn.execute (no RETURNING) after a topic_id fetchval.
    assert conn.execute.await_count == 1
    execute_args = conn.execute.await_args.args
    sql = execute_args[0]
    positional_binds = execute_args[1:]

    # SQL must name user_id in the INSERT column list and target the right table
    assert "recommendation_feedback" in sql
    assert "user_id" in sql, f"Expected 'user_id' in INSERT column list, got SQL:\n{sql}"
    assert "signal" in sql
    assert "source" in sql

    # Binds: $1=paper_id, $2=user_id(None), $3=signal, $4=source, $5=reason, $6=topic_id
    assert len(positional_binds) == 6, (
        f"Expected 6 positional args (paper_id, user_id, signal, source, reason, topic_id), "
        f"got {len(positional_binds)}: {positional_binds}"
    )
    assert positional_binds[0] == 7  # paper_id
    assert positional_binds[1] == 1  # user_id (real authenticated user)
    assert positional_binds[2] == "positive"  # signal
    assert positional_binds[3] == "feed_thumbs"  # source
    assert positional_binds[4] is None  # reason
    assert positional_binds[5] is None  # topic_id (no topic mapped)


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


async def test_submit_feedback_select_returns_correct_user_row_when_monkeypatched():
    """C2: With a real user_id, INSERT binds that user_id so only their row is upserted.

    In multi-tenant mode, ``assert_paper_ownership`` issues 1 fetchrow (ownership
    check), then ``_upsert_recommendation_feedback`` issues 1 fetchval (topic
    lookup) + 1 execute (INSERT...ON CONFLICT). Verifies the actual user_id
    flows through as $2 in the INSERT.
    """
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.side_effect = [
        {"id": 10, "is_visible": True},  # central visibility check passes
        {"discovery_origin": "pulse"},  # feedback gate: system-discovered paper
    ]
    conn.fetchval.return_value = None  # no topic mapping

    # CC-03: caller identity passed explicitly to the direct handler call
    # (pre-conversion this test set the in-body resolver to return 42 via a
    # module-symbol swap; the threaded user_id and every assertion are identical).
    result = await papers.submit_feedback.__wrapped__(
        MagicMock(),
        paper_id=10,
        body=FeedbackRequest(signal="positive", source="paper_detail_thumbs"),
        db_pool=pool,
        user_id=42,
    )

    assert result.signal == "positive"
    assert result.paper_id == 10

    # Ownership check and discovery-origin validation each use fetchrow.
    assert conn.fetchrow.await_count == 2
    assert conn.execute.await_count == 1
    insert_args = conn.execute.await_args.args
    insert_binds = insert_args[1:]
    # $1=paper_id, $2=user_id=42, $3=signal, $4=source, $5=reason, $6=topic_id
    assert insert_binds[0] == 10, f"Expected paper_id=10 at $1, got {insert_binds[0]}"
    assert insert_binds[1] == 42, f"Expected user_id=42 at $2, got {insert_binds[1]}"
