"""Tests for Wave 6A ownership helper: assert_paper_ownership.

Covers the 4-quadrant matrix:
  - single-user mode (user_id=None) always allows
  - multi-user mode + system-owned paper (paper.user_id=NULL) always allows
  - multi-user mode + owner match → allows
  - multi-user mode + owner mismatch → 403
  - multi-user mode + paper missing → 404
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException


def _make_conn(fetchrow_return) -> AsyncMock:
    """Return an asyncpg Connection mock with fetchrow() pre-wired."""
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=fetchrow_return)
    return conn


def _make_row(user_id: int | None) -> MagicMock:
    """Return a mock asyncpg Record for a paper row."""
    row = MagicMock()
    row.__getitem__ = MagicMock(side_effect=lambda key: user_id if key == "user_id" else None)
    return row


# ---------------------------------------------------------------------------
# 404 — paper missing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assert_paper_ownership_404_for_missing_paper() -> None:
    """When the paper does not exist, raise 404."""
    from jarvis_common.db_helpers import assert_paper_ownership

    conn = _make_conn(None)  # fetchrow returns None → paper not found

    with pytest.raises(HTTPException) as exc_info:
        await assert_paper_ownership(conn, paper_id=999, user_id=42)

    assert exc_info.value.status_code == 404
    assert "not found" in exc_info.value.detail


# ---------------------------------------------------------------------------
# Single-user mode (user_id=None) — skip check entirely
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assert_paper_ownership_passes_in_single_user_mode_regardless() -> None:
    """Single-user mode (user_id=None) must pass without any DB query."""
    from jarvis_common.db_helpers import assert_paper_ownership

    conn = AsyncMock()
    conn.fetchrow = AsyncMock()

    # Should not raise, regardless of DB state
    await assert_paper_ownership(conn, paper_id=1, user_id=None)

    # No DB round-trip needed in single-user mode
    conn.fetchrow.assert_not_called()


# ---------------------------------------------------------------------------
# Multi-user mode + system-owned paper (paper.user_id = NULL)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assert_paper_ownership_passes_for_system_owned_paper_in_multi_user_mode() -> None:
    """System-owned papers (paper.user_id=NULL) are accessible to any authenticated user."""
    from jarvis_common.db_helpers import assert_paper_ownership

    row = _make_row(None)  # paper.user_id is NULL → system-owned
    conn = _make_conn(row)

    # Should not raise for any authenticated user
    await assert_paper_ownership(conn, paper_id=1, user_id=99)


# ---------------------------------------------------------------------------
# Multi-user mode + owner match
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assert_paper_ownership_passes_for_owner_match() -> None:
    """Owner calling on their own paper must pass."""
    from jarvis_common.db_helpers import assert_paper_ownership

    row = _make_row(42)  # paper owned by user 42
    conn = _make_conn(row)

    # Caller is also user 42 → should pass
    await assert_paper_ownership(conn, paper_id=7, user_id=42)


# ---------------------------------------------------------------------------
# Multi-user mode + owner mismatch → 403
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assert_paper_ownership_403_for_other_user() -> None:
    """A different user accessing an owned paper must get 403."""
    from jarvis_common.db_helpers import assert_paper_ownership

    row = _make_row(42)  # paper owned by user 42
    conn = _make_conn(row)

    # Caller is user 99 — different from the owner
    with pytest.raises(HTTPException) as exc_info:
        await assert_paper_ownership(conn, paper_id=7, user_id=99)

    assert exc_info.value.status_code == 403
    assert "not owned" in exc_info.value.detail


# ---------------------------------------------------------------------------
# Export check
# ---------------------------------------------------------------------------


def test_assert_paper_ownership_exported_from_jarvis_common() -> None:
    """assert_paper_ownership must be importable from the top-level jarvis_common package."""
    from jarvis_common import assert_paper_ownership  # noqa: F401
