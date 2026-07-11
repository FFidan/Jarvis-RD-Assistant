"""Tests for ``paper_ingestion.jobs.purge_sessions``."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from paper_ingestion.jobs.purge_sessions import (
    _DELETE_STALE_SESSIONS,
    purge_stale_sessions,
)


@pytest.mark.asyncio
async def test_purge_stale_sessions_issues_expected_delete() -> None:
    """The async function calls pool.execute with the canonical DELETE statement."""
    pool: Any = MagicMock()
    pool.execute = AsyncMock(return_value="DELETE 3")

    await purge_stale_sessions(pool)

    pool.execute.assert_awaited_once_with(_DELETE_STALE_SESSIONS)


@pytest.mark.asyncio
async def test_purge_stale_sessions_swallows_db_error(caplog) -> None:
    """A transient DB failure does not propagate (scheduler must not crash)."""
    pool: Any = MagicMock()
    pool.execute = AsyncMock(side_effect=RuntimeError("boom"))

    await purge_stale_sessions(pool)  # must not raise

    assert any("purge_sessions: failed" in rec.message for rec in caplog.records)


def test_delete_statement_targets_expired_and_revoked() -> None:
    """SQL must purge only long-expired and long-revoked sessions."""
    assert "sessions" in _DELETE_STALE_SESSIONS
    assert "expires_at < now() - INTERVAL '30 days'" in _DELETE_STALE_SESSIONS
    assert "revoked_at IS NOT NULL" in _DELETE_STALE_SESSIONS
    assert "revoked_at < now() - INTERVAL '7 days'" in _DELETE_STALE_SESSIONS
