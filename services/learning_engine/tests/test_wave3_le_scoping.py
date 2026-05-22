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

from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.conftest import _make_pool_and_conn, make_pool_and_conn


def _pool_with_conn(conn: AsyncMock) -> MagicMock:
    """Wrap an existing conn in a pool mock (no transaction needed for these tests)."""
    pool, _ = make_pool_and_conn(conn=conn, with_transaction=False)
    return pool


# ---------------------------------------------------------------------------
# 1. intent_repo — cross-user isolation
# ---------------------------------------------------------------------------


# test_get_today_query_does_not_use_null_or_clause deleted — SQL-text B1-09
# ("IS NOT DISTINCT FROM" in sql, "IS NULL OR" not in sql);
# survivor: test_executive_contract.py (A192) verifies intent scoping against
# real PostgreSQL with real NULL-owner row isolation.


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


# test_delete_today_query_does_not_use_null_or_clause deleted — SQL-text B1-09
# ("IS NOT DISTINCT FROM" in sql, "IS NULL OR" not in sql);
# survivor: test_executive_contract.py (A192/A193) verifies intent scoping.


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


# test_get_project_count_queries_include_user_id deleted — SQL-text B1-09
# ("user_id = $2" in counts_sql); survivor: test_projects_contract.py verifies
# task/milestone count scoping against real PostgreSQL.


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


# test_unlink_paper_from_task_uses_single_connection deleted — SQL-text B1-09
# ("user_id = $2" in ownership_sql, "DELETE FROM task_paper_links" in delete_sql);
# survivor: test_tasks_contract.py verifies unlink TOCTOU fix + ownership against
# real PostgreSQL.


@pytest.mark.asyncio
async def test_unlink_paper_from_task_404_for_other_users_task() -> None:
    """User B cannot unlink a paper from user A's task — gets 404."""
    import types

    from fastapi import HTTPException
    from learning_engine.routers.tasks import unlink_paper_from_task

    fake_request = types.SimpleNamespace(state=types.SimpleNamespace(user_id=2))

    # Use _make_pool_and_conn so conn.transaction() is a proper async CM.
    pool, conn = _make_pool_and_conn()
    conn.fetchval.return_value = None  # task not found / not owned by user 2

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
