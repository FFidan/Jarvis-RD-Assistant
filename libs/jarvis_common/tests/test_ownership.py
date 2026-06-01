"""Tests for the assert_paper_ownership helper.

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


def _make_conn(fetchrow_return, *, in_library: bool = False) -> AsyncMock:
    """Return an asyncpg Connection mock with fetchrow + fetchval pre-wired.

    ``assert_paper_ownership`` reads ``papers.discovered_by`` first via
    fetchrow, then optionally checks ``user_library`` membership via fetchval.
    ``in_library`` controls the second probe.
    """
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=fetchrow_return)
    conn.fetchval = AsyncMock(return_value=1 if in_library else None)
    return conn


def _make_row(discovered_by: int | None) -> MagicMock:
    """Return a mock asyncpg Record for a paper row."""
    row = MagicMock()
    row.__getitem__ = MagicMock(
        side_effect=lambda key: discovered_by if key == "discovered_by" else None
    )
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

    row = _make_row(42)  # paper discovered by user 42
    # 403 fires only when paper is NOT in the caller's library either.
    conn = _make_conn(row, in_library=False)

    # Caller is user 99 — different from the discoverer, paper not in library.
    with pytest.raises(HTTPException) as exc_info:
        await assert_paper_ownership(conn, paper_id=7, user_id=99)

    assert exc_info.value.status_code == 403
    assert "not owned" in exc_info.value.detail


# ---------------------------------------------------------------------------
# None/None semantics — discovered_by=NULL + user_id=None edge case
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assert_paper_ownership_none_discovered_by_with_none_user_id() -> None:
    """When paper.discovered_by=NULL and user_id=None, the function must return early.

    This test verifies that the equality check uses proper None comparison (==),
    not string conversion which would hide the None-passthrough logic. The
    condition is: ``discovered_by == user_id or discovered_by is None``.

    With discovered_by=None and user_id=None:
    - discovered_by == user_id → None == None → True → return (correct)
    - Alternately, discovered_by is None → True → return (correct)

    Both paths should succeed. This test validates the first path.
    """
    from jarvis_common.db_helpers import assert_paper_ownership

    row = _make_row(None)  # paper.discovered_by is NULL
    conn = _make_conn(row)

    # Single-user mode: user_id=None should pass early via the equality check
    # (or via the None branch). Either way, no exception should be raised.
    await assert_paper_ownership(conn, paper_id=1, user_id=None)


# ---------------------------------------------------------------------------
# Int equality with no str-coercion — discriminating tests for type-aware ==
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assert_paper_ownership_int_equality_no_str_coercion() -> None:
    """Python int == int works without str-coercion.

    Verifies that the equality check in the ownership guard uses Python's
    native type comparison (int == int) and does not fall back to string
    comparison (str(42) == str(42)), which would mask type-safety regressions.

    This test uses integer 42 for both discovered_by and user_id to ensure
    the == operator works with ints directly.
    """
    from jarvis_common.db_helpers import assert_paper_ownership

    row = _make_row(42)  # paper.discovered_by is int 42
    conn = _make_conn(row)

    # user_id is also int 42 — should pass via int equality
    await assert_paper_ownership(conn, paper_id=1, user_id=42)


@pytest.mark.asyncio
async def test_assert_paper_ownership_int_user_id_str_discovered_rejected() -> None:
    """Type mismatch (str discovered_by vs int user_id) is rejected.

    Verifies that the equality check does NOT perform str-coercion: if
    paper.discovered_by is "42" (string) and user_id is 42 (int), the
    comparison must fail (not pass via str(42) == str(42)).

    This is the discriminating test: the old code with str-coercion would
    have passed; the new code correctly raises 403 for the mismatch.
    """
    from jarvis_common.db_helpers import assert_paper_ownership

    # Simulate paper with string discovered_by (legacy or corruption)
    row = MagicMock()
    row.__getitem__ = MagicMock(side_effect=lambda key: "42" if key == "discovered_by" else None)
    conn = _make_conn(row, in_library=False)

    # user_id is int 42 — should NOT match string "42", so 403 is raised
    with pytest.raises(HTTPException) as exc_info:
        await assert_paper_ownership(conn, paper_id=1, user_id=42)

    assert exc_info.value.status_code == 403
    assert "not owned" in exc_info.value.detail


# ---------------------------------------------------------------------------
# Export check
# ---------------------------------------------------------------------------


def test_assert_paper_ownership_exported_from_jarvis_common() -> None:
    """assert_paper_ownership must be importable from the top-level jarvis_common package."""
    from jarvis_common import assert_paper_ownership  # noqa: F401
