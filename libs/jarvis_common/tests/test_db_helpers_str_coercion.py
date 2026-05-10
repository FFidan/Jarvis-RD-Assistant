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


def _make_conn(
    discovered_by_value: int | str | None,
    *,
    in_library: bool = False,
) -> AsyncMock:
    """Return a mock asyncpg Connection.

    Sprint B canonical-corpus: ``assert_paper_ownership`` first reads
    ``papers.discovered_by`` (audit column). If that matches the caller or
    is NULL (system), access is allowed. Otherwise the helper checks
    ``user_library`` membership for a 403/200 split.
    """
    conn = AsyncMock()
    conn.fetchrow.return_value = {"discovered_by": discovered_by_value}
    conn.fetchval.return_value = 1 if in_library else None
    return conn


@pytest.mark.asyncio
async def test_assert_paper_ownership_uses_string_coercion():
    """discovered_by=42 (int from DB) and user_id='42' (str from caller) must not raise."""
    conn = _make_conn(discovered_by_value=42)  # DB returns int
    # Should not raise — str(42) == str("42")
    await assert_paper_ownership(conn, paper_id=1, user_id="42")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_assert_paper_ownership_int_int_match():
    """Same type comparison (int==int) on discovered_by still works."""
    conn = _make_conn(discovered_by_value=42)
    await assert_paper_ownership(conn, paper_id=1, user_id=42)


@pytest.mark.asyncio
async def test_assert_paper_ownership_str_str_mismatch_raises_403():
    """Different discoverer + paper not in caller's library → 403."""
    conn = _make_conn(discovered_by_value=99, in_library=False)
    with pytest.raises(HTTPException) as exc_info:
        await assert_paper_ownership(conn, paper_id=1, user_id=42)
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_assert_paper_ownership_null_owner_allows_all():
    """System-discovered papers (discovered_by=NULL) remain freely accessible."""
    conn = _make_conn(discovered_by_value=None)
    await assert_paper_ownership(conn, paper_id=1, user_id=42)


@pytest.mark.asyncio
async def test_assert_paper_ownership_in_library_grants_access():
    """Paper discovered by user A but in user B's library → user B has access."""
    conn = _make_conn(discovered_by_value=99, in_library=True)
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
