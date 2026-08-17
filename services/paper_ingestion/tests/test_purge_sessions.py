"""Tests for ``paper_ingestion.jobs.purge_sessions``."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from paper_ingestion.jobs.purge_sessions import purge_stale_sessions


@pytest.mark.asyncio
async def test_purge_stale_sessions_issues_both_deletes() -> None:
    """The async function issues both the sessions DELETE and the challenges DELETE."""
    pool: Any = MagicMock()
    pool.fetchval = AsyncMock(return_value=3)

    await purge_stale_sessions(pool)

    assert pool.fetchval.await_count == 2
    operations = [call.args[1] for call in pool.fetchval.await_args_list]
    assert operations == ["sessions", "webauthn_challenges"]


@pytest.mark.asyncio
async def test_purge_stale_sessions_swallows_db_error(caplog) -> None:
    """A transient DB failure does not propagate (scheduler must not crash)."""
    pool: Any = MagicMock()
    pool.fetchval = AsyncMock(side_effect=RuntimeError("boom"))

    await purge_stale_sessions(pool)  # must not raise

    assert any("purge_sessions: failed" in rec.message for rec in caplog.records)
