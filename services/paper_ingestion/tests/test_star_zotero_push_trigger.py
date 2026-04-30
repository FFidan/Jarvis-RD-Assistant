"""Tests for the zotero.push enqueue side-effect in star_paper.

Phase-A / B6: star_paper enqueues a ``zotero.push`` job iff the paper has
at least one ``project_papers`` row.  The enqueue is best-effort — failures
are logged but must NOT fail the mutation itself.

Grounded against services/paper_ingestion/paper_ingestion/routers/papers.py
lines 614–650 (star_paper body as of 2026-04-30).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from paper_ingestion.routers import papers

# ---------------------------------------------------------------------------
# Helpers — mirror test_papers_lifecycle.py pattern exactly
# ---------------------------------------------------------------------------


def _conn_with_txn():
    """AsyncMock connection that also supports nested transactions."""
    conn = AsyncMock()
    txn_cm = MagicMock()
    txn_cm.__aenter__ = AsyncMock(return_value=txn_cm)
    txn_cm.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn_cm)
    return conn


def _pool(conn):
    """Wrap a mock conn in an asyncpg-style pool acquire() context manager."""
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx
    return pool


def _make_pool_and_conn():
    conn = _conn_with_txn()
    pool = _pool(conn)
    return pool, conn


def _mock_request():
    return MagicMock()


# ---------------------------------------------------------------------------
# Scenario 1 — no project links → enqueue must NOT be called
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_star_no_project_links_does_not_enqueue():
    """COUNT(*) from project_papers returns 0 → jobs_lib.enqueue is never awaited."""
    pool, conn = _make_pool_and_conn()

    # fetchrow: SELECT id FROM papers → paper exists
    conn.fetchrow.return_value = {"id": 10}
    # fetchval: SELECT COUNT(*) FROM project_papers → 0 links
    conn.fetchval.return_value = 0

    with (
        patch(
            "paper_ingestion.routers.papers.current_user_id_or_none",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "paper_ingestion.routers.papers.assert_paper_ownership",
            new_callable=AsyncMock,
        ),
        patch(
            "paper_ingestion.routers.papers._upsert_state_and_starred",
            new_callable=AsyncMock,
        ),
        patch(
            "paper_ingestion.routers.papers.jobs_lib.enqueue",
            new_callable=AsyncMock,
        ) as mock_enqueue,
    ):
        result = await papers.star_paper.__wrapped__(_mock_request(), paper_id=10, db_pool=pool)

    assert result == {"status": "ok", "paper_id": 10}
    mock_enqueue.assert_not_awaited()


# ---------------------------------------------------------------------------
# Scenario 2 — has project links → enqueue called exactly once
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_star_with_project_links_enqueues_zotero_push():
    """COUNT(*) from project_papers returns 2 → jobs_lib.enqueue is awaited once with correct args."""
    pool, conn = _make_pool_and_conn()

    conn.fetchrow.return_value = {"id": 10}
    # Two project_papers rows → should trigger enqueue
    conn.fetchval.return_value = 2

    with (
        patch(
            "paper_ingestion.routers.papers.current_user_id_or_none",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "paper_ingestion.routers.papers.assert_paper_ownership",
            new_callable=AsyncMock,
        ),
        patch(
            "paper_ingestion.routers.papers._upsert_state_and_starred",
            new_callable=AsyncMock,
        ),
        patch(
            "paper_ingestion.routers.papers.jobs_lib.enqueue",
            new_callable=AsyncMock,
        ) as mock_enqueue,
    ):
        result = await papers.star_paper.__wrapped__(_mock_request(), paper_id=10, db_pool=pool)

    assert result == {"status": "ok", "paper_id": 10}
    # First positional arg is the pool itself; second is the job name; third is the payload
    mock_enqueue.assert_awaited_once_with(pool, "zotero.push", {"paper_id": 10})


# ---------------------------------------------------------------------------
# Scenario 3 — enqueue raises → handler still returns 200, logger.exception called
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_star_enqueue_failure_is_best_effort():
    """If jobs_lib.enqueue raises, star_paper must return 200 and log via logger.exception."""
    pool, conn = _make_pool_and_conn()

    conn.fetchrow.return_value = {"id": 10}
    conn.fetchval.return_value = 1  # one project link → triggers enqueue

    with (
        patch(
            "paper_ingestion.routers.papers.current_user_id_or_none",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "paper_ingestion.routers.papers.assert_paper_ownership",
            new_callable=AsyncMock,
        ),
        patch(
            "paper_ingestion.routers.papers._upsert_state_and_starred",
            new_callable=AsyncMock,
        ),
        patch(
            "paper_ingestion.routers.papers.jobs_lib.enqueue",
            new_callable=AsyncMock,
            side_effect=RuntimeError("queue down"),
        ),
        patch.object(papers.logger, "exception") as mock_log_exc,
    ):
        # Must NOT raise
        result = await papers.star_paper.__wrapped__(_mock_request(), paper_id=10, db_pool=pool)

    assert result == {"status": "ok", "paper_id": 10}
    mock_log_exc.assert_called_once()
