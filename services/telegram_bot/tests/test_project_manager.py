"""Unit tests for the Telegram project manager service layer."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.project_manager import ProjectManager


def _row(**values):
    """Return a dict-like row payload for asyncpg mocks."""
    return values


def _make_pool_with_conn(conn: AsyncMock) -> MagicMock:
    """Create a pool mock whose acquire() yields the provided connection."""
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx
    return pool


@pytest.mark.asyncio
async def test_list_projects_filters_by_status():
    """list_projects forwards the status filter and returns plain dicts."""
    db_pool = AsyncMock()
    db_pool.fetch.return_value = [_row(id=1, name="A", status="active")]
    manager = ProjectManager(db_pool)

    result = await manager.list_projects(status="active")

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
