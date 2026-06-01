"""Tests for ``paper_ingestion.jobs.purge_tokens``."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from paper_ingestion.jobs.purge_tokens import (
    _DELETE_EXPIRED_TOKENS,
    purge_expired_magic_link_tokens,
)


@pytest.mark.asyncio
async def test_purge_expired_magic_link_tokens_issues_expected_delete() -> None:
    """The async function calls pool.execute with the canonical DELETE statement."""
    pool: Any = MagicMock()
    pool.execute = AsyncMock(return_value="DELETE 7")

    await purge_expired_magic_link_tokens(pool)

    pool.execute.assert_awaited_once_with(_DELETE_EXPIRED_TOKENS)


@pytest.mark.asyncio
async def test_purge_expired_magic_link_tokens_swallows_db_error(caplog) -> None:
    """A transient DB failure does not propagate (scheduler must not crash)."""
    pool: Any = MagicMock()
    pool.execute = AsyncMock(side_effect=RuntimeError("boom"))

    await purge_expired_magic_link_tokens(pool)  # must not raise

    assert any("purge_tokens: failed" in rec.message for rec in caplog.records)


def test_delete_statement_uses_one_day_grace() -> None:
    """SQL must purge rows with expires_at < NOW() - INTERVAL '1 day' (grace window)."""
    assert "magic_link_tokens" in _DELETE_EXPIRED_TOKENS
    assert "NOW() - INTERVAL '1 day'" in _DELETE_EXPIRED_TOKENS
