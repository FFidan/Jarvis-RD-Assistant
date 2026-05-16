"""Wave-3 learning_engine defence-in-depth scoping tests.

Covers:
1. intent_repo cross-user isolation — user B cannot read/delete user A's intent
   or a NULL-owner row when their user_id differs.
2. get_project child counts — task/milestone counts are filtered by user_id.
3. get_my_day project pulse — milestone laterals carry user_id filter.
4. unlink_paper_from_task TOCTOU fix — ownership check and DELETE run on one
   connection (single pool.acquire() call).
"""

from __future__ import annotations

import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Shared helpers (mirror existing test patterns)
# ---------------------------------------------------------------------------


class _Acquire:
    """Async context manager returning a fake DB connection."""

    def __init__(self, conn: AsyncMock) -> None:
        self.conn = conn

    async def __aenter__(self) -> AsyncMock:
        return self.conn

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


def _pool_with_conn(conn: AsyncMock) -> MagicMock:
    pool = MagicMock()
    pool.acquire.return_value = _Acquire(conn)
    return pool


def _make_pool_and_conn() -> tuple[MagicMock, AsyncMock]:
    """Return (pool, conn) — same pattern used across learning_engine tests."""
    conn = AsyncMock()
    txn_cm = MagicMock()
    txn_cm.__aenter__ = AsyncMock(return_value=txn_cm)
    txn_cm.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn_cm)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx
    return pool, conn


# ---------------------------------------------------------------------------
# 1. intent_repo — cross-user isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_today_query_does_not_use_null_or_clause() -> None:
    """get_today must NOT include 'user_id IS NULL OR' in its WHERE clause.

    The old query let ANY user read a NULL-owner row.  After the fix the
    predicate is strictly 'WHERE user_id IS NOT DISTINCT FROM $1'.
    """
    from learning_engine.repos.intent_repo import get_today

    conn = AsyncMock()
    conn.fetchrow.return_value = None
    pool = _pool_with_conn(conn)

    await get_today(pool, user_id=42)

    conn.fetchrow.assert_awaited_once()
    sql: str = conn.fetchrow.call_args.args[0]
    assert "IS NOT DISTINCT FROM" in sql, "predicate must use IS NOT DISTINCT FROM"
    assert "IS NULL OR" not in sql, "NULL-owner bypass must not be present"
    assert "user_id IS NULL OR" not in sql


@pytest.mark.asyncio
async def test_get_today_user_b_cannot_read_null_owner_row() -> None:
    """With the fixed query, user B (id=2) fetches with $1=2.

    A NULL-owner row would only match IS NOT DISTINCT FROM 2 if 2 IS NULL,
    which is false, so the DB returns nothing.  We simulate that here.
    """
    from learning_engine.repos.intent_repo import get_today

    conn = AsyncMock()
    # DB returns no row because NULL IS NOT DISTINCT FROM 2 is false.
    conn.fetchrow.return_value = None
    pool = _pool_with_conn(conn)

    result = await get_today(pool, user_id=2)

    assert result == {"intent": None, "updated_at": None}
    # Verify the bound parameter is 2, not some broadened form.
    bound_user_id = conn.fetchrow.call_args.args[1]
    assert bound_user_id == 2


@pytest.mark.asyncio
async def test_delete_today_query_does_not_use_null_or_clause() -> None:
    """delete_today must NOT include 'user_id IS NULL OR' in its WHERE clause."""
    from learning_engine.repos.intent_repo import delete_today

    conn = AsyncMock()
    conn.execute.return_value = "DELETE 0"
    pool = _pool_with_conn(conn)

    await delete_today(pool, user_id=7)

    conn.execute.assert_awaited_once()
    sql: str = conn.execute.call_args.args[0]
    assert "IS NOT DISTINCT FROM" in sql
    assert "IS NULL OR" not in sql
    bound_user_id = conn.execute.call_args.args[1]
    assert bound_user_id == 7


@pytest.mark.asyncio
async def test_delete_today_binds_caller_user_id() -> None:
    """delete_today binds the caller's user_id so it only deletes their own row."""
    from learning_engine.repos.intent_repo import delete_today

    conn = AsyncMock()
    conn.execute.return_value = "DELETE 1"
    pool = _pool_with_conn(conn)

    await delete_today(pool, user_id=99)

    bound_user_id = conn.execute.call_args.args[1]
    assert bound_user_id == 99


# ---------------------------------------------------------------------------
# 2. get_project — child count subqueries include user_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_project_count_queries_include_user_id() -> None:
    """The lateral subqueries for tasks/milestones must filter by user_id.

    Previously the counts subqueries only filtered by project_id, so user B
    could see counts for tasks/milestones owned by user A within the same
    project.
    """

    from learning_engine.routers.projects import get_project

    class _FakeRecord(dict):
        def __getattr__(self, name):
            try:
                return self[name]
            except KeyError as e:
                raise AttributeError(name) from e

        def keys(self):
            return super().keys()

    _now = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    project_row = _FakeRecord(
        id=1,
        user_id=5,
        name="P",
        description=None,
        color="#fff",
        status="active",
        created_at=_now,
        updated_at=_now,
    )
    counts_row = _FakeRecord(
        total_tasks=3,
        done_tasks=1,
        total_milestones=2,
        completed_milestones=0,
        paper_count=0,
        open_question_count=0,
    )

    conn = AsyncMock()
    # First fetchrow → project ownership check; second → counts
    conn.fetchrow.side_effect = [project_row, counts_row]

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx

    import types

    fake_request = types.SimpleNamespace(state=types.SimpleNamespace(user_id=5))

    # Call the unwrapped handler (bypass limiter decorator)
    await get_project.__wrapped__(
        fake_request,
        project_id=1,
        db_pool=pool,
        user_id=5,
    )

    # The second fetchrow call is the counts query — verify user_id is bound.
    counts_call = conn.fetchrow.call_args_list[1]
    counts_sql: str = counts_call.args[0]
    counts_args = counts_call.args[1:]

    assert "user_id = $2" in counts_sql, "counts subqueries must filter by user_id"
    assert 5 in counts_args, "user_id value must be bound in counts query"


@pytest.mark.asyncio
async def test_get_project_returns_404_for_other_users_project() -> None:
    """User B requesting user A's project gets 404."""
    from fastapi import HTTPException
    from learning_engine.routers.projects import get_project

    conn = AsyncMock()
    conn.fetchrow.return_value = None  # no row matching user B's user_id

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx

    import types

    fake_request = types.SimpleNamespace(state=types.SimpleNamespace(user_id=2))

    with pytest.raises(HTTPException) as exc_info:
        await get_project.__wrapped__(
            fake_request,
            project_id=1,
            db_pool=pool,
            user_id=2,
        )

    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# 3. get_my_day — milestone laterals carry user_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_my_day_milestone_laterals_include_user_id() -> None:
    """The project_pulse milestone sub-selects must filter by m.user_id = $1.

    Confirms the SQL text contains AND m.user_id = $1 for both the name and
    deadline milestone laterals.
    """
    # B7 hoisted the /my-day SQL into module-level constants and runs the six
    # reads concurrently; the project-pulse query (with the milestone laterals)
    # now lives in executive._PROJECT_PULSE_SQL rather than inline in
    # get_my_day. The user_id scoping invariant is unchanged — assert it
    # against the SQL constant directly.
    from learning_engine.routers.executive import _PROJECT_PULSE_SQL

    assert "m.user_id = $1" in _PROJECT_PULSE_SQL, (
        "milestone laterals in _PROJECT_PULSE_SQL must filter by m.user_id = $1"
    )
    # And there should be two occurrences (name lateral + deadline lateral).
    assert _PROJECT_PULSE_SQL.count("m.user_id = $1") >= 2, (
        "both milestone sub-selects (name and deadline) must add m.user_id = $1"
    )


# ---------------------------------------------------------------------------
# 4. unlink_paper_from_task — single connection (TOCTOU fix)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unlink_paper_from_task_uses_single_connection() -> None:
    """Ownership check and DELETE must share one connection (one acquire call).

    The TOCTOU fix collapses the previously separate 'async with pool.acquire()'
    calls for the check and the delete into a single context-manager block.
    """
    import types

    from learning_engine.routers.tasks import unlink_paper_from_task

    fake_request = types.SimpleNamespace(state=types.SimpleNamespace(user_id=10))

    conn = AsyncMock()
    conn.fetchval.return_value = 5  # task exists and is owned by user 10
    conn.execute.return_value = "DELETE 1"

    # Wire a single shared ctx so we can count acquire() calls.
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx

    # log_audit uses pool too; patch it out.
    from unittest.mock import patch

    with patch("learning_engine.routers.tasks.log_audit", new=AsyncMock()):
        await unlink_paper_from_task.__wrapped__(
            fake_request,
            task_id=5,
            paper_id=3,
            db_pool=pool,
            user_id=10,
        )

    # The TOCTOU fix means pool.acquire() is called only ONCE for both the
    # ownership check and the DELETE (log_audit gets its own acquire, which is
    # patched out above).
    assert pool.acquire.call_count == 1, (
        f"Expected 1 pool.acquire() call (single connection for check+delete), "
        f"got {pool.acquire.call_count}"
    )

    # Verify both the ownership SELECT and the DELETE were issued on that conn.
    conn.fetchval.assert_awaited_once()
    ownership_sql: str = conn.fetchval.call_args.args[0]
    assert "tasks" in ownership_sql
    assert "user_id = $2" in ownership_sql

    conn.execute.assert_awaited_once()
    delete_sql: str = conn.execute.call_args.args[0]
    assert "DELETE FROM task_paper_links" in delete_sql


@pytest.mark.asyncio
async def test_unlink_paper_from_task_404_for_other_users_task() -> None:
    """User B cannot unlink a paper from user A's task — gets 404."""
    import types

    from fastapi import HTTPException
    from learning_engine.routers.tasks import unlink_paper_from_task

    fake_request = types.SimpleNamespace(state=types.SimpleNamespace(user_id=2))

    conn = AsyncMock()
    conn.fetchval.return_value = None  # task not found / not owned by user 2

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx

    with pytest.raises(HTTPException) as exc_info:
        await unlink_paper_from_task.__wrapped__(
            fake_request,
            task_id=5,
            paper_id=3,
            db_pool=pool,
            user_id=2,
        )

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Task not found"
    # DELETE must NOT have been called.
    conn.execute.assert_not_awaited()
