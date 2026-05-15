"""D4 regression: pin the canonical-corpus ownership contract.

Decision (D4, 2026-05-15)
--------------------------
The bibliographic corpus is intentionally shared across all users on an
instance. Papers with ``discovered_by IS NULL`` are system/instance papers
(ingested by Pulse, schedulers, Zotero sync) and are globally readable by
every authenticated user — no ``user_library`` row required.  Per-user
isolation is enforced on the activity/output layer (library, notes, cards,
projects, ratings, intent).  The prior ``multitenant_enabled`` knob was
removed to make this explicit and untoggleable.  See docs/SECURITY.md §
"Data Sharing Boundary" for the full guarantee.

What these tests guard
-----------------------
* ``discovered_by == caller`` → always allowed.
* ``discovered_by IS NULL`` → always allowed (shared canonical paper).
* ``discovered_by == other_user``, NOT in caller's ``user_library`` → 403.
* Both the singular (``assert_paper_ownership``) and batch
  (``assert_papers_ownership``) helpers must enforce the same contract.

If a future change reintroduces corpus-level per-user gating, these tests
WILL fail loudly.  Update them only alongside an explicit D4-reversal decision
and the corresponding back-fill migration.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from jarvis_common.db_helpers import assert_paper_ownership, assert_papers_ownership


def _conn(*, discovered_by: int | None, in_library: bool = False) -> AsyncMock:
    """asyncpg.Connection mock for the singular helper."""
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"discovered_by": discovered_by})
    conn.fetchval = AsyncMock(return_value=1 if in_library else None)
    return conn


def _batch_conn(
    *,
    rows: list[dict[str, object]],
    owned_paper_ids: list[int] | None = None,
) -> AsyncMock:
    """asyncpg.Connection mock for the plural helper.

    First ``conn.fetch`` returns the paper rows; second returns the
    ``user_library`` membership rows for the candidate set.
    """
    conn = AsyncMock()
    conn.fetch = AsyncMock(
        side_effect=[
            rows,
            [{"paper_id": pid} for pid in (owned_paper_ids or [])],
        ]
    )
    return conn


# ---------------------------------------------------------------------------
# Singular — D4 contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_null_discovered_by_is_global_free_pass() -> None:
    """INVARIANT: discovered_by IS NULL → allowed for any authenticated user.

    This is the exact path every production caller takes for a
    system-discovered (canonical-corpus) paper.  It MUST NOT 403, and the
    user_library probe must not run (the free pass short-circuits).
    """
    conn = _conn(discovered_by=None, in_library=False)

    await assert_paper_ownership(conn, paper_id=1, user_id=42)

    conn.fetchval.assert_not_awaited()


@pytest.mark.asyncio
async def test_same_owner_is_allowed() -> None:
    """discovered_by == caller → allowed (caller's own paper)."""
    conn = _conn(discovered_by=42, in_library=False)

    await assert_paper_ownership(conn, paper_id=1, user_id=42)

    conn.fetchval.assert_not_awaited()


@pytest.mark.asyncio
async def test_other_users_paper_not_in_library_is_403() -> None:
    """discovered_by == other user, NOT in caller's library → 403.

    Guards against over-broad "simplification" that would weaken
    cross-user isolation while touching this code.
    """
    conn = _conn(discovered_by=7, in_library=False)

    with pytest.raises(HTTPException) as exc_info:
        await assert_paper_ownership(conn, paper_id=1, user_id=42)

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_other_users_paper_in_library_is_allowed() -> None:
    """discovered_by == other user, but in caller's user_library → allowed."""
    conn = _conn(discovered_by=7, in_library=True)

    await assert_paper_ownership(conn, paper_id=1, user_id=42)


@pytest.mark.asyncio
async def test_single_user_mode_skips_all_checks() -> None:
    """user_id=None (single-user/ops mode) → no DB query, no raise."""
    conn = _conn(discovered_by=7, in_library=False)

    await assert_paper_ownership(conn, paper_id=1, user_id=None)

    conn.fetchrow.assert_not_awaited()
    conn.fetchval.assert_not_awaited()


# ---------------------------------------------------------------------------
# Plural — same D4 contract for the batch helper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_null_discovered_by_free_pass() -> None:
    """INVARIANT (batch): all rows NULL discovered_by → allowed, no user_library round-trip."""
    conn = _batch_conn(
        rows=[
            {"id": 1, "discovered_by": None},
            {"id": 2, "discovered_by": None},
        ],
        owned_paper_ids=[],
    )

    await assert_papers_ownership(conn, [1, 2], user_id=42)

    # Only the papers SELECT ran; the user_library SELECT was skipped because
    # every paper hit the global free pass.
    assert conn.fetch.await_count == 1


@pytest.mark.asyncio
async def test_batch_other_users_paper_not_in_library_is_403() -> None:
    """Batch: discovered_by == other user, not in library → 403."""
    conn = _batch_conn(
        rows=[{"id": 1, "discovered_by": 7}],
        owned_paper_ids=[],
    )

    with pytest.raises(HTTPException) as exc_info:
        await assert_papers_ownership(conn, [1], user_id=42)

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_batch_mixed_null_and_owned_no_403() -> None:
    """Batch: mix of NULL (shared) + caller-owned papers → allowed, no 403."""
    conn = _batch_conn(
        rows=[
            {"id": 1, "discovered_by": None},
            {"id": 2, "discovered_by": 42},
        ],
        owned_paper_ids=[],
    )

    await assert_papers_ownership(conn, [1, 2], user_id=42)

    # Both papers hit the fast-grant; no user_library query needed.
    assert conn.fetch.await_count == 1
