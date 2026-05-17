"""Tests for the WS-USER-DELETION daily user-purge job."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from paper_ingestion.jobs.data_purge import (
    _PURGE_SQL,
    data_purge_task,
    register_data_purge,
)


@pytest.mark.asyncio
async def test_purge_deletes_only_users_past_grace() -> None:
    pool = AsyncMock()
    pool.execute.return_value = "DELETE 3"
    app = MagicMock()
    app.state.db_pool = pool

    with patch("jarvis_common.event_log.log_event", new_callable=AsyncMock) as mock_log:
        await data_purge_task(app)

    sql = pool.execute.call_args[0][0]
    assert "deleted_at IS NOT NULL" in sql
    assert "deleted_at < NOW() - INTERVAL '30 days'" in sql
    assert sql == _PURGE_SQL
    mock_log.assert_called_once()
    assert "3" in mock_log.call_args[1]["message"]


@pytest.mark.asyncio
async def test_purge_no_rows_skips_event_log() -> None:
    pool = AsyncMock()
    pool.execute.return_value = "DELETE 0"
    app = MagicMock()
    app.state.db_pool = pool

    with patch("jarvis_common.event_log.log_event", new_callable=AsyncMock) as mock_log:
        await data_purge_task(app)

    mock_log.assert_not_called()


@pytest.mark.asyncio
async def test_purge_handles_pool_exception() -> None:
    pool = AsyncMock()
    pool.execute.side_effect = RuntimeError("db down")
    app = MagicMock()
    app.state.db_pool = pool

    with patch("paper_ingestion.jobs.data_purge.logger") as mock_logger:
        await data_purge_task(app)  # must not raise
        mock_logger.exception.assert_called_once()


def test_register_data_purge_adds_daily_cron_job() -> None:
    scheduler = MagicMock()
    app = MagicMock()
    register_data_purge(scheduler, app)
    scheduler.add_job.assert_called_once()
    kwargs = scheduler.add_job.call_args.kwargs
    assert kwargs["id"] == "data_purge"
    assert kwargs["max_instances"] == 1
    assert kwargs["replace_existing"] is True
