"""Unit tests for the Telegram project manager service layer."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from jarvis_common.testing import make_pool_and_conn
from telegram_bot.project_manager import ProjectManager


def _row(**values):
    """Return a dict-like row payload for asyncpg mocks."""
    return values


def _make_pool_with_conn(conn: AsyncMock) -> MagicMock:
    """Create a pool mock whose acquire() yields the provided connection."""
    pool, _ = make_pool_and_conn(conn=conn, with_transaction=False)
    return pool


@pytest.mark.asyncio
async def test_complete_task_updates_daily_log_when_task_changes():
    """complete_task marks the task done and increments today's daily log."""
    conn = AsyncMock()
    conn.fetchrow.return_value = _row(id=4, status="done")
    conn.transaction = MagicMock(
        return_value=MagicMock(
            __aenter__=AsyncMock(return_value=None),
            __aexit__=AsyncMock(return_value=False),
        )
    )
    manager = ProjectManager(_make_pool_with_conn(conn))

    result = await manager.complete_task(4)

    assert result == {"id": 4, "status": "done"}
    executed_sql = [call.args[0] for call in conn.execute.await_args_list]
    assert any("INSERT INTO daily_log" in sql for sql in executed_sql)
    assert any("SET tasks_completed = tasks_completed + 1" in sql for sql in executed_sql)


@pytest.mark.asyncio
async def test_complete_task_scopes_update_by_user_id():
    """complete_task with user_id refuses to touch other users' tasks (C2 fix)."""
    conn = AsyncMock()
    # No row returned: task wasn't owned by user_id=7
    conn.fetchrow.return_value = None
    conn.transaction = MagicMock(
        return_value=MagicMock(
            __aenter__=AsyncMock(return_value=None),
            __aexit__=AsyncMock(return_value=False),
        )
    )
    manager = ProjectManager(_make_pool_with_conn(conn))

    result = await manager.complete_task(4, user_id=7)

    assert result == {}
    # The UPDATE must carry both task_id and user_id as parameters.
    update_call = conn.fetchrow.await_args
    assert "user_id IS NOT DISTINCT FROM" in update_call.args[0]
    assert update_call.args[1:] == (4, 7)
    # Daily-log writes should NOT run when the UPDATE matched zero rows.
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_complete_task_daily_log_inserts_user_id():
    """complete_task with user_id writes user-scoped daily_log rows (C2 fix)."""
    conn = AsyncMock()
    conn.fetchrow.return_value = _row(id=4, status="done", user_id=7)
    conn.transaction = MagicMock(
        return_value=MagicMock(
            __aenter__=AsyncMock(return_value=None),
            __aexit__=AsyncMock(return_value=False),
        )
    )
    manager = ProjectManager(_make_pool_with_conn(conn))

    await manager.complete_task(4, user_id=7)

    executed = list(conn.execute.await_args_list)
    insert_sql, insert_params = executed[0].args[0], executed[0].args[1:]
    update_sql, update_params = executed[1].args[0], executed[1].args[1:]
    assert "INSERT INTO daily_log (log_date, user_id)" in insert_sql
    assert "ON CONFLICT (user_id, log_date)" in insert_sql
    assert insert_params[1] == 7
    assert "user_id IS NOT DISTINCT FROM" in update_sql
    assert update_params[1] == 7


@pytest.mark.asyncio
async def test_create_project_persists_user_id():
    """create_project writes user_id into the projects row (C2 fix)."""
    db_pool = AsyncMock()
    db_pool.fetchrow.return_value = _row(id=99, name="Mine", user_id=7)
    manager = ProjectManager(db_pool)

    result = await manager.create_project("Mine", user_id=7)

    assert result["id"] == 99
    insert_sql = db_pool.fetchrow.await_args.args[0]
    insert_params = db_pool.fetchrow.await_args.args[1:]
    assert "user_id" in insert_sql
    assert insert_params == ("Mine", None, None, 7)


@pytest.mark.asyncio
async def test_create_project_defaults_user_id_to_null():
    """create_project without user_id writes NULL — legacy owner semantics."""
    db_pool = AsyncMock()
    db_pool.fetchrow.return_value = _row(id=100, name="Legacy", user_id=None)
    manager = ProjectManager(db_pool)

    await manager.create_project("Legacy")

    insert_params = db_pool.fetchrow.await_args.args[1:]
    assert insert_params == ("Legacy", None, None, None)
