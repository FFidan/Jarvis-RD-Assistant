"""Unit tests for the Telegram project manager service layer."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
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
async def test_list_projects_filters_by_status():
    """list_projects forwards the status filter and returns plain dicts."""
    db_pool = AsyncMock()
    db_pool.fetch.return_value = [_row(id=1, name="A", status="active")]
    manager = ProjectManager(db_pool)

    result = await manager.list_projects(user_id=1, status="active")

    assert result == [{"id": 1, "name": "A", "status": "active"}]
    assert db_pool.fetch.await_args.args[1] == "active"


@pytest.mark.asyncio
async def test_update_project_status_rejects_invalid_values():
    """update_project_status validates the allowed status set."""
    manager = ProjectManager(AsyncMock())

    with pytest.raises(ValueError, match="Invalid status"):
        await manager.update_project_status(1, "broken")


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


@pytest.mark.asyncio
async def test_update_daily_log_rejects_disallowed_fields():
    """update_daily_log only permits the documented increment keys."""
    manager = ProjectManager(AsyncMock())

    with pytest.raises(ValueError, match="Disallowed field"):
        await manager.update_daily_log(unexpected_field=1)


@pytest.mark.asyncio
async def test_update_daily_log_applies_only_non_zero_increments():
    """update_daily_log skips zero increments and returns the updated row."""
    conn = AsyncMock()
    conn.fetchrow.return_value = _row(
        log_date=datetime.now(UTC).date(),
        tasks_completed=2,
        cards_reviewed=0,
        papers_read=1,
    )
    conn.transaction = MagicMock(
        return_value=MagicMock(
            __aenter__=AsyncMock(return_value=None),
            __aexit__=AsyncMock(return_value=False),
        )
    )
    manager = ProjectManager(_make_pool_with_conn(conn))

    result = await manager.update_daily_log(tasks_completed=2, cards_reviewed=0)

    assert result["tasks_completed"] == 2
    executed_sql = [call.args[0] for call in conn.execute.await_args_list]
    assert any("INSERT INTO daily_log" in sql for sql in executed_sql)
    assert sum("UPDATE daily_log SET" in sql for sql in executed_sql) == 1


@pytest.mark.asyncio
async def test_get_project_papers_returns_plain_dicts():
    """get_project_papers maps database rows to serializable dicts."""
    db_pool = AsyncMock()
    db_pool.fetch.return_value = [
        _row(id=10, title="Paper", note="useful", task_title="Implement"),
    ]
    manager = ProjectManager(db_pool)

    result = await manager.get_project_papers(3)

    assert result == [{"id": 10, "title": "Paper", "note": "useful", "task_title": "Implement"}]


def test_list_projects_requires_user_id() -> None:
    """list_projects must accept user_id with no default (SEC-PRJMGR-1)."""
    from telegram_bot.project_manager import ProjectManager

    sig = inspect.signature(ProjectManager.list_projects)
    assert "user_id" in sig.parameters, "list_projects must have user_id parameter"
    assert sig.parameters["user_id"].default is inspect.Parameter.empty, (
        "user_id must be mandatory (no default)"
    )


def test_get_today_tasks_requires_user_id() -> None:
    """get_today_tasks must accept user_id with no default (SEC-PRJMGR-1)."""
    from telegram_bot.project_manager import ProjectManager

    sig = inspect.signature(ProjectManager.get_today_tasks)
    assert "user_id" in sig.parameters
    assert sig.parameters["user_id"].default is inspect.Parameter.empty


def test_get_upcoming_milestones_requires_user_id() -> None:
    """get_upcoming_milestones must accept user_id with no default (SEC-PRJMGR-1)."""
    from telegram_bot.project_manager import ProjectManager

    sig = inspect.signature(ProjectManager.get_upcoming_milestones)
    assert "user_id" in sig.parameters
    assert sig.parameters["user_id"].default is inspect.Parameter.empty
