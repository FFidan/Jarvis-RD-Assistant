"""Tests for jarvis_common.event_log.log_event."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest
from jarvis_common.event_log import log_event
from jarvis_common.logging_config import correlation_id_var

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pool(
    *, raise_on_acquire: bool = False, raise_type: type = asyncpg.PostgresError
) -> tuple[MagicMock, AsyncMock]:
    pool = MagicMock()
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value=None)

    if raise_on_acquire:
        pool.acquire.return_value.__aenter__ = AsyncMock(side_effect=raise_type("db down"))
        pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    else:
        pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
        pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    return pool, conn


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_log_event_inserts_row_with_all_fields():
    """log_event should call conn.execute with all expected positional args."""
    pool, conn = _make_pool()
    test_uuid = uuid.uuid4()

    await log_event(
        pool=pool,
        level="error",
        category="auth",
        source="test.module",
        message="something failed",
        context={"key": "value"},
        correlation_id=test_uuid,
    )

    conn.execute.assert_awaited_once()
    call_args = conn.execute.call_args
    # Positional args after the SQL string: level, category, source, message, context_json, correlation_id
    _, pos_args = call_args[0][0], call_args[0][1:]
    assert pos_args[0] == "error"
    assert pos_args[1] == "auth"
    assert pos_args[2] == "test.module"
    assert pos_args[3] == "something failed"
    assert '"key": "value"' in pos_args[4]
    assert pos_args[5] == test_uuid


@pytest.mark.asyncio
async def test_log_event_picks_up_correlation_from_contextvar_when_not_passed():
    """When correlation_id is not passed, log_event should use correlation_id_var."""
    pool, conn = _make_pool()
    test_uuid = uuid.uuid4()

    token = correlation_id_var.set(test_uuid)
    try:
        await log_event(
            pool=pool,
            level="info",
            category="job",
            source="test.module",
            message="ctx test",
        )
    finally:
        correlation_id_var.reset(token)

    conn.execute.assert_awaited_once()
    call_args = conn.execute.call_args[0]
    # Last positional arg is correlation_id
    assert call_args[-1] == test_uuid


@pytest.mark.asyncio
async def test_log_event_does_not_raise_when_pool_unavailable():
    """log_event must swallow asyncpg.PostgresError and OSError and log a warning."""
    pool, _ = _make_pool(raise_on_acquire=True, raise_type=asyncpg.PostgresError)

    with patch("jarvis_common.event_log.logger") as mock_logger:
        # Should not raise
        await log_event(
            pool=pool,
            level="warning",
            category="error",
            source="test.module",
            message="won't persist",
        )
        mock_logger.warning.assert_called_once()
        warning_msg = mock_logger.warning.call_args[0][0]
        assert "log_event failed" in warning_msg


@pytest.mark.asyncio
async def test_log_event_does_not_raise_when_oserror():
    """log_event must also swallow OSError (e.g. network gone)."""
    pool, _ = _make_pool(raise_on_acquire=True, raise_type=OSError)

    with patch("jarvis_common.event_log.logger") as mock_logger:
        await log_event(
            pool=pool,
            level="critical",
            category="config",
            source="test.module",
            message="os error test",
        )
        mock_logger.warning.assert_called_once()
