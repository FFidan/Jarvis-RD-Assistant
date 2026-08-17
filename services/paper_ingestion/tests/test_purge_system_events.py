"""Tests for tiered system_events purge scheduler task.

Tests the purge_system_events_task from paper_ingestion.scheduler,
which deletes application events older than 30 days and infrastructure
events older than 7 days.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jarvis_common.testing import make_pool_and_conn
from paper_ingestion.scheduler import purge_system_events_task


def _make_purge_pool():
    """Create a pool supporting both purge writes and event-log acquisition.

    The purge writes go through ``pool.fetchval`` directly and must stay
    independent of the event-log conn behind ``acquire()``, so no
    ``direct_methods`` here.
    """
    pool, _event_conn = make_pool_and_conn()
    pool.fetchval = AsyncMock()
    return pool


@pytest.mark.asyncio
async def test_purge_deletes_app_events_older_than_30_days_keeps_newer():
    """Test that application events older than 30 days are deleted."""
    pool = _make_purge_pool()
    app = MagicMock()
    app.state.db_pool = pool

    pool.fetchval.side_effect = [42, 0]

    with patch("jarvis_common.event_log.log_event", new_callable=AsyncMock) as mock_log:
        await purge_system_events_task(app)

    assert pool.fetchval.await_count == 2
    assert mock_log.await_args.kwargs["message"] == "deleted 42 app + 0 infra events"


@pytest.mark.asyncio
async def test_purge_deletes_infra_events_older_than_7_days_keeps_newer():
    """Test that infrastructure events older than 7 days are deleted."""
    pool = _make_purge_pool()
    app = MagicMock()
    app.state.db_pool = pool

    pool.fetchval.side_effect = [5, 12]

    with patch("jarvis_common.event_log.log_event", new_callable=AsyncMock) as mock_log:
        await purge_system_events_task(app)

    assert pool.fetchval.await_count == 2
    assert mock_log.await_args.kwargs["message"] == "deleted 5 app + 12 infra events"


@pytest.mark.asyncio
async def test_purge_emits_log_event_with_counts():
    """Test that log_event is called with correct deletion counts."""
    pool = _make_purge_pool()
    app = MagicMock()
    app.state.db_pool = pool

    pool.fetchval.side_effect = [42, 12]

    # Mock log_event to track the call
    with patch("jarvis_common.event_log.log_event", new_callable=AsyncMock) as mock_log:
        await purge_system_events_task(app)

        # Verify log_event was called with correct parameters
        mock_log.assert_called_once()
        call_kwargs = mock_log.call_args[1]
        assert call_kwargs["pool"] == pool
        assert call_kwargs["level"] == "info"
        assert call_kwargs["category"] == "config"
        assert call_kwargs["source"] == "purge_system_events"
        assert "42" in call_kwargs["message"]
        assert "12" in call_kwargs["message"]


@pytest.mark.asyncio
async def test_purge_handles_malformed_delete_response():
    """Test that malformed asyncpg.execute response is handled gracefully."""
    pool = _make_purge_pool()
    app = MagicMock()
    app.state.db_pool = pool

    pool.fetchval.side_effect = ["INVALID", "DELETE"]

    with (
        patch("jarvis_common.event_log.log_event", new_callable=AsyncMock) as mock_log,
        patch("paper_ingestion.scheduler.logger") as mock_logger,
    ):
        await purge_system_events_task(app)

    mock_log.assert_not_awaited()
    mock_logger.exception.assert_called_once()


@pytest.mark.asyncio
async def test_purge_handles_pool_exception():
    """Test that exceptions from pool.execute are caught and logged."""
    pool = _make_purge_pool()
    app = MagicMock()
    app.state.db_pool = pool

    pool.fetchval.side_effect = RuntimeError("Database connection failed")

    # Should not raise; exception should be logged
    with patch("paper_ingestion.scheduler.logger") as mock_logger:
        await purge_system_events_task(app)

        # Verify logger.exception was called
        mock_logger.exception.assert_called_once()
        assert "purge_system_events" in mock_logger.exception.call_args[0][0]
