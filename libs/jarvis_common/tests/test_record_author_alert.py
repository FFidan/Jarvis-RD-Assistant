"""Unit tests for db_helpers.record_author_alert.

Covers:
  (a) fetchrow returns a row → helper returns True (newly inserted).
  (b) fetchrow returns None → helper returns False (ON CONFLICT skipped).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


def _conn(fetchrow_return) -> AsyncMock:
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=fetchrow_return)
    return conn


@pytest.mark.asyncio
async def test_record_author_alert_returns_true_on_new_insert() -> None:
    """When the INSERT succeeds (no conflict), fetchrow returns a row → True."""
    from jarvis_common.db_helpers import record_author_alert

    row = MagicMock()
    row.__getitem__ = MagicMock(return_value=7)
    conn = _conn(row)

    result = await record_author_alert(conn, tracked_author_id=7, paper_id=42, user_id=1)

    assert result is True
    conn.fetchrow.assert_awaited_once()


@pytest.mark.asyncio
async def test_record_author_alert_returns_false_on_conflict_skip() -> None:
    """When ON CONFLICT skips the insert, fetchrow returns None → False."""
    from jarvis_common.db_helpers import record_author_alert

    conn = _conn(None)

    result = await record_author_alert(conn, tracked_author_id=7, paper_id=42, user_id=1)

    assert result is False
    conn.fetchrow.assert_awaited_once()
