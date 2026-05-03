"""Tests for the zotero.push enqueue side-effect in star_paper.

Phase-A / A.3: star_paper enqueues a ``zotero.push`` job iff BOTH:
  - the paper has at least one ``project_papers`` row, AND
  - ``zotero.auto_push_on_star`` is ``True`` in ``user_config``.

The enqueue is best-effort — failures are logged but must NOT fail the
mutation itself.

Grounded against services/paper_ingestion/paper_ingestion/routers/papers.py
(star_paper body).
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
# (auto_push_on_star=True but no project links → no enqueue)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_star_no_project_links_does_not_enqueue():
    """COUNT(*) from project_papers returns 0 → jobs_lib.enqueue is never awaited.

    auto_push_on_star=True but project_link_count=0 → still no enqueue.
    """
    pool, conn = _make_pool_and_conn()

    # fetchrow: SELECT id FROM papers → paper exists
    conn.fetchrow.return_value = {"id": 10}
    # fetchval: call 1 = paper_user_state.starred prior → None (no row yet)
    #           call 2 = COUNT(*) project_papers → 0 links
    #           call 3 = user_config zotero.auto_push_on_star → True
    conn.fetchval.side_effect = [None, 0, True]

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
            "jarvis_common.task_registry.zotero_push.defer_async",
            new_callable=AsyncMock,
        ) as mock_enqueue,
    ):
        result = await papers.star_paper.__wrapped__(_mock_request(), paper_id=10, db_pool=pool)

    assert result == {"status": "ok", "paper_id": 10}
    mock_enqueue.assert_not_awaited()


# ---------------------------------------------------------------------------
# Scenario 2 — has project links AND auto_push_on_star=True → enqueue called
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_star_with_project_links_and_toggle_on_enqueues_zotero_push():
    """COUNT(*) returns 2 AND auto_push_on_star=True → jobs_lib.enqueue awaited once."""
    pool, conn = _make_pool_and_conn()

    conn.fetchrow.return_value = {"id": 10}
    # fetchval: call 1 = prior starred → None, call 2 = COUNT(*) → 2, call 3 = auto_push_on_star → True
    conn.fetchval.side_effect = [None, 2, True]

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
            "jarvis_common.task_registry.zotero_push.defer_async",
            new_callable=AsyncMock,
        ) as mock_enqueue,
    ):
        result = await papers.star_paper.__wrapped__(_mock_request(), paper_id=10, db_pool=pool)

    assert result == {"status": "ok", "paper_id": 10}
    mock_enqueue.assert_awaited_once()
    call_kwargs = mock_enqueue.await_args.kwargs
    assert call_kwargs.get("paper_id") == 10
    assert "job_id" in call_kwargs


# ---------------------------------------------------------------------------
# Scenario 3 — has project links BUT auto_push_on_star=False → no enqueue
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_star_with_project_links_toggle_off_does_not_enqueue():
    """COUNT(*) returns 1 but auto_push_on_star=False → jobs_lib.enqueue NOT awaited."""
    pool, conn = _make_pool_and_conn()

    conn.fetchrow.return_value = {"id": 10}
    # fetchval: call 1 = prior starred → None, call 2 = COUNT(*) → 1, call 3 = auto_push_on_star → False
    conn.fetchval.side_effect = [None, 1, False]

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
            "jarvis_common.task_registry.zotero_push.defer_async",
            new_callable=AsyncMock,
        ) as mock_enqueue,
    ):
        result = await papers.star_paper.__wrapped__(_mock_request(), paper_id=10, db_pool=pool)

    assert result == {"status": "ok", "paper_id": 10}
    mock_enqueue.assert_not_awaited()


# ---------------------------------------------------------------------------
# Scenario 4 — has project links BUT auto_push_on_star not set (None) → no enqueue
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_star_with_project_links_toggle_not_set_does_not_enqueue():
    """COUNT(*) returns 1 but auto_push_on_star key absent (None) → no enqueue.

    Default-off: if the key is missing from user_config the feature is disabled.
    """
    pool, conn = _make_pool_and_conn()

    conn.fetchrow.return_value = {"id": 10}
    # fetchval: call 1 = prior starred → None, call 2 = COUNT(*) → 1, call 3 = auto_push_on_star → None (key absent)
    conn.fetchval.side_effect = [None, 1, None]

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
            "jarvis_common.task_registry.zotero_push.defer_async",
            new_callable=AsyncMock,
        ) as mock_enqueue,
    ):
        result = await papers.star_paper.__wrapped__(_mock_request(), paper_id=10, db_pool=pool)

    assert result == {"status": "ok", "paper_id": 10}
    mock_enqueue.assert_not_awaited()


# ---------------------------------------------------------------------------
# Scenario 5 — enqueue raises → handler still returns 200, logger.exception called
# (auto_push_on_star=True + project links → enqueue fails gracefully)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_star_enqueue_failure_is_best_effort():
    """If jobs_lib.enqueue raises, star_paper must return 200 and log via logger.exception."""
    pool, conn = _make_pool_and_conn()

    conn.fetchrow.return_value = {"id": 10}
    # fetchval: call 1 = prior starred → None, call 2 = COUNT(*) → 1, call 3 = auto_push_on_star → True
    conn.fetchval.side_effect = [None, 1, True]

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
            "jarvis_common.task_registry.zotero_push.defer_async",
            new=AsyncMock(side_effect=RuntimeError("queue down")),
        ),
        patch.object(papers.logger, "exception") as mock_log_exc,
    ):
        # Must NOT raise
        result = await papers.star_paper.__wrapped__(_mock_request(), paper_id=10, db_pool=pool)

    assert result == {"status": "ok", "paper_id": 10}
    mock_log_exc.assert_called_once()


# ---------------------------------------------------------------------------
# Scenario 6 — paper already starred → off→on transition guard prevents
# double-enqueue on client retry / double-tap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_star_already_starred_does_not_double_enqueue():
    """Repeat /star call on a paper already starred → enqueue NOT awaited.

    Even with project_link_count>0 and auto_push_on_star=True, the off→on
    transition guard (was_unstarred=False) skips the enqueue. This prevents
    duplicate Zotero pushes from client retries or double-tapped UI.
    """
    pool, conn = _make_pool_and_conn()

    conn.fetchrow.return_value = {"id": 10}
    # fetchval: call 1 = prior starred → True (already starred), call 2 = COUNT(*) → 1, call 3 = auto_push_on_star → True
    conn.fetchval.side_effect = [True, 1, True]

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
            "jarvis_common.task_registry.zotero_push.defer_async",
            new_callable=AsyncMock,
        ) as mock_enqueue,
    ):
        result = await papers.star_paper.__wrapped__(_mock_request(), paper_id=10, db_pool=pool)

    assert result == {"status": "ok", "paper_id": 10}
    mock_enqueue.assert_not_awaited()
