"""Tests for C1/C2 user_id threading in mark_paper_read and submit_feedback.

C1: mark_paper_read INSERT must include user_id to avoid clobbering starred rows.
C2: submit_feedback INSERT must include user_id; SELECT must filter by user_id.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
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
# C1: mark_paper_read — user_id must be threaded into INSERT
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mark_paper_read_does_not_clobber_starred_state():
    """C1: mark_paper_read INSERT must include user_id in column list and VALUES.

    If user_id were omitted, the ON CONFLICT target (paper_id, user_id) would
    match the NULL-user starred row and overwrite its status with 'read',
    silently destroying the star bookmark.

    This test verifies:
    1. The INSERT SQL includes 'user_id' in the column list.
    2. The INSERT SQL values bind are ($1, $2, 'read') — user_id as $2.
    3. The execute call receives exactly (paper_id, user_id) as positional args.
    """
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = {"id": 7}  # paper-exists check

    result = await papers.mark_paper_read.__wrapped__(
        MagicMock(),
        paper_id=7,
        db_pool=pool,
    )

    assert result == {"status": "ok", "paper_id": 7}

    # Verify exactly one execute call happened
    assert conn.execute.await_count == 1

    execute_call_args = conn.execute.await_args.args
    sql = execute_call_args[0]
    positional_binds = execute_call_args[1:]

    # SQL must name user_id in the INSERT column list
    assert "user_id" in sql, f"Expected 'user_id' in INSERT column list, got SQL:\n{sql}"

    # The INSERT column list must include all three columns
    assert "paper_id" in sql
    assert "status" in sql

    # Positional bind args: $1=paper_id, $2=user_id (None in stub mode)
    # In stub mode current_user_id_or_none returns None
    assert len(positional_binds) == 2, (
        f"Expected 2 positional args (paper_id, user_id), got {len(positional_binds)}: "
        f"{positional_binds}"
    )
    assert positional_binds[0] == 7, f"First arg should be paper_id=7, got {positional_binds[0]}"
    # Second arg is user_id — None in stub/single-tenant mode
    assert positional_binds[1] is None, (
        f"Second arg should be user_id=None (stub mode), got {positional_binds[1]}"
    )


@pytest.mark.asyncio
async def test_mark_paper_read_threads_user_id_for_non_null_user():
    """C1: When current_user_id_or_none returns a real user_id, it is threaded into INSERT.

    Simulates multi-tenant mode by monkeypatching current_user_id_or_none.
    assert_paper_ownership fetches 'SELECT user_id FROM papers WHERE id=$1' first
    (ownership check), then mark_paper_read fetches 'SELECT id FROM papers WHERE id=$1'
    (existence check). We use side_effect to supply both rows in order.
    """
    pool, conn = _make_pool_and_conn()
    # First fetchrow: ownership check returns a row where user_id matches caller (99)
    # Second fetchrow: paper-exists check returns {"id": 42}
    conn.fetchrow.side_effect = [{"user_id": 99}, {"id": 42}]

    async def _user_99(_request):
        return 99

    import paper_ingestion.routers.papers as papers_module

    original = papers_module.current_user_id_or_none
    papers_module.current_user_id_or_none = _user_99
    try:
        result = await papers.mark_paper_read.__wrapped__(
            MagicMock(),
            paper_id=42,
            db_pool=pool,
        )
    finally:
        papers_module.current_user_id_or_none = original

    assert result == {"status": "ok", "paper_id": 42}

    execute_call_args = conn.execute.await_args.args
    positional_binds = execute_call_args[1:]

    # user_id=99 must be the second positional arg
    assert positional_binds == (42, 99), (
        f"Expected (paper_id=42, user_id=99), got {positional_binds}"
    )


# ---------------------------------------------------------------------------
# C2: submit_feedback — user_id must be in INSERT and SELECT
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_feedback_threads_user_id_to_insert():
    """C2: submit_feedback INSERT must include user_id as $2, shifting rating/flagged to $3/$4."""
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = {"rating": 4, "flagged": False}

    result = await papers.submit_feedback.__wrapped__(
        MagicMock(),
        paper_id=7,
        feedback=FeedbackRequest(rating=4, flagged=None),
        db_pool=pool,
    )

    assert result["paper_id"] == 7
    assert result["status"] == "updated"

    # First await_args is the INSERT execute call
    assert conn.execute.await_count == 1
    execute_args = conn.execute.await_args.args
    sql = execute_args[0]
    positional_binds = execute_args[1:]

    # SQL must name user_id in the INSERT column list
    assert "user_id" in sql, f"Expected 'user_id' in INSERT column list, got SQL:\n{sql}"
    assert "rating" in sql
    assert "flagged" in sql

    # In stub mode: $1=paper_id, $2=user_id(None), $3=rating, $4=flagged
    assert len(positional_binds) == 4, (
        f"Expected 4 positional args (paper_id, user_id, rating, flagged), "
        f"got {len(positional_binds)}: {positional_binds}"
    )
    assert positional_binds[0] == 7  # paper_id
    assert positional_binds[1] is None  # user_id (stub mode → None)
    assert positional_binds[2] == 4  # rating
    assert positional_binds[3] is None  # flagged


@pytest.mark.asyncio
async def test_submit_feedback_threads_user_id_to_select():
    """C2: submit_feedback SELECT must use IS NOT DISTINCT FROM $2 with user_id bound.

    In stub mode (user_id=None) the IS NOT DISTINCT FROM clause matches NULL rows,
    ensuring single-tenant compatibility while being correct for multi-tenant.
    """
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = {"rating": 3, "flagged": True}

    await papers.submit_feedback.__wrapped__(
        MagicMock(),
        paper_id=10,
        feedback=FeedbackRequest(rating=3, flagged=None),
        db_pool=pool,
    )

    # fetchrow is the SELECT after the INSERT
    assert conn.fetchrow.await_count == 1
    fetchrow_args = conn.fetchrow.await_args.args
    select_sql = fetchrow_args[0]
    select_binds = fetchrow_args[1:]

    # SELECT must filter by user_id using IS NOT DISTINCT FROM
    assert "IS NOT DISTINCT FROM" in select_sql, (
        f"Expected 'IS NOT DISTINCT FROM' in SELECT, got SQL:\n{select_sql}"
    )
    assert "user_id" in select_sql

    # Two binds: $1=paper_id, $2=user_id
    assert len(select_binds) == 2, (
        f"Expected 2 positional args (paper_id, user_id), got {len(select_binds)}: {select_binds}"
    )
    assert select_binds[0] == 10  # paper_id
    assert select_binds[1] is None  # user_id (stub mode → None)


@pytest.mark.asyncio
async def test_submit_feedback_select_returns_correct_user_row_when_monkeypatched():
    """C2: With a real user_id, SELECT binds that user_id so only their row is returned.

    Verifies that the SELECT call passes the actual user_id as second positional
    arg, not None, when running in multi-tenant mode.

    assert_paper_ownership issues fetchrow #1 (ownership check).
    submit_feedback then issues fetchrow #2 (SELECT after INSERT).
    """
    pool, conn = _make_pool_and_conn()
    # fetchrow #1: ownership check — user_id=42 owns the paper
    # fetchrow #2: post-INSERT SELECT returns the state row
    conn.fetchrow.side_effect = [{"user_id": 42}, {"rating": 5, "flagged": False}]

    async def _user_42(_request):
        return 42

    import paper_ingestion.routers.papers as papers_module

    original = papers_module.current_user_id_or_none
    papers_module.current_user_id_or_none = _user_42
    try:
        result = await papers.submit_feedback.__wrapped__(
            MagicMock(),
            paper_id=10,
            feedback=FeedbackRequest(rating=5, flagged=None),
            db_pool=pool,
        )
    finally:
        papers_module.current_user_id_or_none = original

    assert result["rating"] == 5

    # fetchrow was called twice; the last call is the SELECT after INSERT
    assert conn.fetchrow.await_count == 2
    fetchrow_args = conn.fetchrow.await_args.args  # last call args
    select_binds = fetchrow_args[1:]
    assert select_binds == (10, 42), (
        f"Expected SELECT binds (paper_id=10, user_id=42), got {select_binds}"
    )
