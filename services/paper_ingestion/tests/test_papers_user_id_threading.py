"""Tests for C2 user_id threading in submit_feedback.

C2: submit_feedback INSERT must include user_id; conflict key must include user_id.

Note: C1 (mark_paper_read) tests were removed in Phase-A lifecycle redesign because
the mark_paper_read endpoint was deleted (replaced by annotate_paper / state machine).

After Phase-A Wave 1cd, ``submit_feedback`` delegates the write to
``_upsert_recommendation_feedback`` which:
  * issues ``conn.fetchval(...)`` to look up the paper's primary topic_id
    (when not supplied) — 1 fetchval call always
  * issues ``conn.execute(...)`` for the INSERT...ON CONFLICT — 1 execute call,
    no RETURNING (the endpoint synthesises its own ``FeedbackResponse`` from
    the request body + ``datetime.now(UTC)``)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from paper_ingestion.models import FeedbackRequest
from paper_ingestion.routers import papers


def _make_pool_and_conn():
    """Create a mock pool whose acquire() returns an async context manager."""
    conn = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx
    return pool, conn


# ---------------------------------------------------------------------------
# C2: submit_feedback — user_id must be in INSERT and SELECT
# ---------------------------------------------------------------------------


async def test_submit_feedback_threads_user_id_to_insert():
    """C2: submit_feedback INSERT must include user_id as $2 (signal/source/reason after it).

    Writes to recommendation_feedback with binds ($1=paper_id, $2=user_id,
    $3=signal, $4=source, $5=reason, $6=topic_id). In single-user mode
    ``user_id=None`` and ``assert_paper_ownership`` short-circuits, so the only
    DB calls observed are ``fetchval`` (topic lookup) + ``execute`` (INSERT).
    """
    pool, conn = _make_pool_and_conn()
    # fetchval returns topic_id from paper_topics lookup; None means no topic
    conn.fetchval.return_value = None

    result = await papers.submit_feedback.__wrapped__(
        MagicMock(),
        paper_id=7,
        body=FeedbackRequest(signal="positive", source="feed_thumbs"),
        db_pool=pool,
    )

    assert result.paper_id == 7
    assert result.signal == "positive"

    # In single-user mode (user_id=None) there is no ownership fetchrow.
    assert conn.fetchrow.await_count == 0
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
    assert positional_binds[1] is None  # user_id (stub mode → None)
    assert positional_binds[2] == "positive"  # signal
    assert positional_binds[3] == "feed_thumbs"  # source
    assert positional_binds[4] is None  # reason
    assert positional_binds[5] is None  # topic_id (no topic mapped)


async def test_submit_feedback_threads_user_id_to_select():
    """C2: submit_feedback INSERT uses ON CONFLICT keyed on (paper_id, user_id, source).

    Verifies that user_id appears in the conflict target SQL so repeat submissions
    by different users never overwrite each other.  In single-user mode user_id=None
    is bound at $2 — the ON CONFLICT predicate still matches NULL rows correctly
    (PostgreSQL NULL IS NOT DISTINCT FROM NULL).
    """
    pool, conn = _make_pool_and_conn()
    conn.fetchval.return_value = None  # no topic mapping

    await papers.submit_feedback.__wrapped__(
        MagicMock(),
        paper_id=10,
        body=FeedbackRequest(signal="negative", source="pulse_thumbs"),
        db_pool=pool,
    )

    assert conn.execute.await_count == 1
    execute_args = conn.execute.await_args.args
    insert_sql = execute_args[0]

    # The INSERT must use ON CONFLICT keyed on user_id so different users
    # can each store their own signal for the same paper.
    assert "ON CONFLICT" in insert_sql, f"Expected 'ON CONFLICT' in INSERT SQL, got:\n{insert_sql}"
    assert "user_id" in insert_sql

    # Positional bind $2 must be user_id (None in stub mode)
    positional_binds = execute_args[1:]
    assert positional_binds[1] is None  # user_id at $2 (stub mode → None)


async def test_submit_feedback_select_returns_correct_user_row_when_monkeypatched():
    """C2: With a real user_id, INSERT binds that user_id so only their row is upserted.

    In multi-tenant mode, ``assert_paper_ownership`` issues 1 fetchrow (ownership
    check), then ``_upsert_recommendation_feedback`` issues 1 fetchval (topic
    lookup) + 1 execute (INSERT...ON CONFLICT). Verifies the actual user_id
    flows through as $2 in the INSERT.
    """
    pool, conn = _make_pool_and_conn()
    # fetchrow #1: ownership check — user_id=42 owns the paper
    conn.fetchrow.return_value = {"user_id": 42}
    conn.fetchval.return_value = None  # no topic mapping

    async def _user_42(_request):
        return 42

    import paper_ingestion.routers.papers as papers_module

    original = papers_module.current_user_id_or_none
    papers_module.current_user_id_or_none = _user_42
    try:
        result = await papers.submit_feedback.__wrapped__(
            MagicMock(),
            paper_id=10,
            body=FeedbackRequest(signal="positive", source="paper_detail_thumbs"),
            db_pool=pool,
        )
    finally:
        papers_module.current_user_id_or_none = original

    assert result.signal == "positive"
    assert result.paper_id == 10

    # Ownership check uses fetchrow once; INSERT uses execute once.
    assert conn.fetchrow.await_count == 1
    assert conn.execute.await_count == 1
    insert_args = conn.execute.await_args.args
    insert_binds = insert_args[1:]
    # $1=paper_id, $2=user_id=42, $3=signal, $4=source, $5=reason, $6=topic_id
    assert insert_binds[0] == 10, f"Expected paper_id=10 at $1, got {insert_binds[0]}"
    assert insert_binds[1] == 42, f"Expected user_id=42 at $2, got {insert_binds[1]}"
