"""Tests for tiered system_events purge scheduler task.

Tests the purge_system_events_task from paper_ingestion.scheduler,
which deletes application events older than 30 days and infrastructure
events older than 7 days.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from paper_ingestion.scheduler import purge_system_events_task


def _make_pool_and_conn():
    """Create a pool supporting both purge writes and event-log acquisition."""
    pool = MagicMock()
    pool.execute = AsyncMock()
    event_conn = AsyncMock()
    acquire_context = MagicMock()
    acquire_context.__aenter__ = AsyncMock(return_value=event_conn)
    acquire_context.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=acquire_context)
    return pool


@pytest.mark.asyncio
async def test_purge_deletes_app_events_older_than_30_days_keeps_newer():
    """Test that application events older than 30 days are deleted."""
    pool = _make_pool_and_conn()
    app = MagicMock()
    app.state.db_pool = pool

    # Mock the execute calls to return deletion counts
    # asyncpg.execute returns "DELETE <n>" format
    pool.execute.side_effect = [
        "DELETE 42",  # app events deleted
        "DELETE 0",  # no infra events deleted
    ]

    await purge_system_events_task(app)

    # Verify execute was called with correct DELETE statements
    assert pool.execute.call_count == 2

    calls = pool.execute.call_args_list
    assert "category != 'infra'" in calls[0][0][0]
    assert "30 days" in calls[0][0][0]
    assert "category = 'infra'" in calls[1][0][0]
    assert "7 days" in calls[1][0][0]


@pytest.mark.asyncio
async def test_purge_deletes_infra_events_older_than_7_days_keeps_newer():
    """Test that infrastructure events older than 7 days are deleted."""
    pool = _make_pool_and_conn()
    app = MagicMock()
    app.state.db_pool = pool

    # Mock the execute calls
    pool.execute.side_effect = [
        "DELETE 5",  # app events deleted
        "DELETE 12",  # infra events deleted
    ]

    await purge_system_events_task(app)

    # Verify execute was called with correct DELETE statements
    assert pool.execute.call_count == 2

    calls = pool.execute.call_args_list
    # First call: app events (category != 'infra', 30 days)
    assert "category != 'infra'" in calls[0][0][0]
    assert "30 days" in calls[0][0][0]
    # Second call: infra events (category = 'infra', 7 days)
    assert "category = 'infra'" in calls[1][0][0]
    assert "7 days" in calls[1][0][0]


@pytest.mark.asyncio
async def test_purge_emits_log_event_with_counts():
    """Test that log_event is called with correct deletion counts."""
    pool = _make_pool_and_conn()
    app = MagicMock()
    app.state.db_pool = pool

    # Mock the execute calls
    pool.execute.side_effect = [
        "DELETE 42",  # app events
        "DELETE 12",  # infra events
    ]

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
    pool = _make_pool_and_conn()
    app = MagicMock()
    app.state.db_pool = pool

    # Mock execute with malformed responses (no count at end)
    pool.execute.side_effect = [
        "INVALID",  # bad response
        "DELETE",  # no count
    ]

    # Should not raise; log_event should be called with -1 counts
    with patch("jarvis_common.event_log.log_event", new_callable=AsyncMock) as mock_log:
        await purge_system_events_task(app)

        mock_log.assert_called_once()
        call_kwargs = mock_log.call_args[1]
        # Both counts should be -1 due to parse failure
        assert "-1" in call_kwargs["message"]


@pytest.mark.asyncio
async def test_purge_handles_pool_exception():
    """Test that exceptions from pool.execute are caught and logged."""
    pool = _make_pool_and_conn()
    app = MagicMock()
    app.state.db_pool = pool

    # Make execute raise an exception
    pool.execute.side_effect = RuntimeError("Database connection failed")

    # Should not raise; exception should be logged
    with patch("paper_ingestion.scheduler.logger") as mock_logger:
        await purge_system_events_task(app)

        # Verify logger.exception was called
        mock_logger.exception.assert_called_once()
        assert "purge_system_events" in mock_logger.exception.call_args[0][0]
