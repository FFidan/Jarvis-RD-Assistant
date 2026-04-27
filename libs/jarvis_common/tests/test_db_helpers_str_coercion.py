"""Tests for H18 fix: assert_paper_ownership uses str() coercion for owner comparison.

Before the fix, paper_owner=42 (int) != user_id="42" (str) would raise 403
even though they represent the same user.  After the fix, str(42) == str("42")
so the legitimate owner is allowed through.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from jarvis_common.db_helpers import assert_paper_ownership


def _make_conn(user_id_value: int | str | None) -> AsyncMock:
    """Return a mock asyncpg Connection whose fetchrow returns user_id_value."""
    conn = AsyncMock()
    conn.fetchrow.return_value = {"user_id": user_id_value}
    return conn


@pytest.mark.asyncio
async def test_assert_paper_ownership_uses_string_coercion():
    """paper_owner=42 (int from DB) and user_id='42' (str from caller) must not raise.

    H18: assert_paper_ownership must use str() coercion on both sides so that
    asyncpg int vs caller str mismatches don't result in a false 403.
    """
    conn = _make_conn(user_id_value=42)  # DB returns int
    # Should not raise — str(42) == str("42")
    await assert_paper_ownership(conn, paper_id=1, user_id="42")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_assert_paper_ownership_int_int_match():
    """Same type comparison (int==int) must still work after the fix."""
    conn = _make_conn(user_id_value=42)
    # Should not raise
    await assert_paper_ownership(conn, paper_id=1, user_id=42)


@pytest.mark.asyncio
async def test_assert_paper_ownership_str_str_mismatch_raises_403():
    """Different owners must still raise 403."""
    conn = _make_conn(user_id_value=99)
    with pytest.raises(HTTPException) as exc_info:
        await assert_paper_ownership(conn, paper_id=1, user_id=42)
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_assert_paper_ownership_null_owner_allows_all():
    """System-owned papers (user_id=NULL) must be accessible to any caller."""
    conn = _make_conn(user_id_value=None)
    # Should not raise — NULL owner = system-owned, accessible to all
    await assert_paper_ownership(conn, paper_id=1, user_id=42)


@pytest.mark.asyncio
async def test_assert_paper_ownership_single_user_mode_skips_check():
    """When caller user_id=None (single-user mode), check is skipped entirely."""
    conn = AsyncMock()
    # fetchrow should NOT be called — single-user mode exits early
    await assert_paper_ownership(conn, paper_id=1, user_id=None)
    conn.fetchrow.assert_not_called()


@pytest.mark.asyncio
async def test_assert_paper_ownership_paper_not_found_raises_404():
    """Missing paper must raise 404."""
    conn = AsyncMock()
    conn.fetchrow.return_value = None
    with pytest.raises(HTTPException) as exc_info:
        await assert_paper_ownership(conn, paper_id=9999, user_id=42)
    assert exc_info.value.status_code == 404
