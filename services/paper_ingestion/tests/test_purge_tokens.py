"""Tests for ``paper_ingestion.jobs.purge_tokens``."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from paper_ingestion.jobs.purge_tokens import purge_expired_magic_link_tokens


@pytest.mark.asyncio
async def test_purge_expired_magic_link_tokens_issues_expected_delete() -> None:
    """The async function calls pool.execute with the canonical DELETE statement."""
    pool: Any = MagicMock()
    pool.fetchval = AsyncMock(return_value=7)

    await purge_expired_magic_link_tokens(pool)

    assert pool.fetchval.await_args.args[1] == "magic_link_tokens"


@pytest.mark.asyncio
async def test_purge_expired_magic_link_tokens_swallows_db_error(caplog) -> None:
    """A transient DB failure does not propagate (scheduler must not crash)."""
    pool: Any = MagicMock()
    pool.fetchval = AsyncMock(side_effect=RuntimeError("boom"))

    await purge_expired_magic_link_tokens(pool)  # must not raise

    assert any("purge_tokens: failed" in rec.message for rec in caplog.records)
