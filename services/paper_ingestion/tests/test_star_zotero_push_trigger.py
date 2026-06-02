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

import jarvis_common.task_registry as task_registry
import pytest
from paper_ingestion.routers import papers
from paper_ingestion.routers import papers_lifecycle

from tests.conftest import _make_pool_and_conn


def _mock_zotero_push_task():
    """Return a (mock_task, mock_defer) pair for zotero.push patching."""
    mock_task = MagicMock()
    mock_enqueue = AsyncMock()
    mock_task.defer_async = mock_enqueue
    return mock_task, mock_enqueue


# ---------------------------------------------------------------------------
# Helpers — mirror test_papers_lifecycle.py pattern exactly
# ---------------------------------------------------------------------------


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
    Group B: star_paper uses CTE+RETURNING fetchrow (not _upsert_state_and_starred).
    fetchrow call 1 = paper existence; call 2 = CTE RETURNING (is_new_row, prev_starred).
    fetchval call 1 = COUNT(*) project_papers; call 2 = auto_push_on_star config.
    """
    pool, conn = _make_pool_and_conn()

    # fetchrow[0]: CTE RETURNING → new star (off→on transition)
    conn.fetchrow.side_effect = [
        # assert_paper_ownership: discovered_by == caller → fast-grant (user_id=1).
        {"discovered_by": 1},
        {"is_new_row": True, "prev_starred": False},
    ]
    # fetchval[0] = COUNT(*) project_papers → 0 links; fetchval[1] = auto_push_on_star → True
    conn.fetchval.side_effect = [0, True]

    mock_task, mock_enqueue = _mock_zotero_push_task()
    with patch.dict(task_registry._TASK_MAP, {"zotero.push": mock_task}):
        result = await papers.star_paper.__wrapped__(
            _mock_request(), paper_id=10, db_pool=pool, user_id=1
        )

    assert result == {"status": "ok", "paper_id": 10}
    mock_enqueue.assert_not_awaited()


# ---------------------------------------------------------------------------
# Scenario 2 — has project links AND auto_push_on_star=True → enqueue called
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_star_with_project_links_and_toggle_on_enqueues_zotero_push():
    """COUNT(*) returns 2 AND auto_push_on_star=True → jobs_lib.enqueue awaited once.

    Group B: star_paper uses CTE+RETURNING fetchrow (not _upsert_state_and_starred).
    """
    pool, conn = _make_pool_and_conn()

    # fetchrow[0]: CTE RETURNING (new off→on star)
    conn.fetchrow.side_effect = [
        # assert_paper_ownership: discovered_by == caller → fast-grant (user_id=1).
        {"discovered_by": 1},
        {"is_new_row": True, "prev_starred": False},
    ]
    # fetchval[0] = COUNT(*) → 2 links; fetchval[1] = auto_push_on_star → True
    conn.fetchval.side_effect = [2, True]

    mock_task, mock_enqueue = _mock_zotero_push_task()
    with patch.dict(task_registry._TASK_MAP, {"zotero.push": mock_task}):
        result = await papers.star_paper.__wrapped__(
            _mock_request(), paper_id=10, db_pool=pool, user_id=1
        )

    assert result == {"status": "ok", "paper_id": 10}
    mock_enqueue.assert_awaited_once()
    assert mock_enqueue.await_args is not None
    call_kwargs = mock_enqueue.await_args.kwargs
    assert call_kwargs.get("paper_id") == 10
    assert "job_id" in call_kwargs


# ---------------------------------------------------------------------------
# Scenario 3 — has project links BUT auto_push_on_star=False → no enqueue
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_star_with_project_links_toggle_off_does_not_enqueue():
    """COUNT(*) returns 1 but auto_push_on_star=False → jobs_lib.enqueue NOT awaited.

    Group B: star_paper uses CTE+RETURNING fetchrow (not _upsert_state_and_starred).
    """
    pool, conn = _make_pool_and_conn()

    # fetchrow[0]: CTE RETURNING
    conn.fetchrow.side_effect = [
        # assert_paper_ownership: discovered_by == caller → fast-grant (user_id=1).
        {"discovered_by": 1},
        {"is_new_row": True, "prev_starred": False},
    ]
    # fetchval[0] = COUNT(*) → 1 link; fetchval[1] = auto_push_on_star → False
    conn.fetchval.side_effect = [1, False]

    mock_task, mock_enqueue = _mock_zotero_push_task()
    with patch.dict(task_registry._TASK_MAP, {"zotero.push": mock_task}):
        result = await papers.star_paper.__wrapped__(
            _mock_request(), paper_id=10, db_pool=pool, user_id=1
        )

    assert result == {"status": "ok", "paper_id": 10}
    mock_enqueue.assert_not_awaited()


# ---------------------------------------------------------------------------
# Scenario 4 — has project links BUT auto_push_on_star not set (None) → no enqueue
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_star_with_project_links_toggle_not_set_does_not_enqueue():
    """COUNT(*) returns 1 but auto_push_on_star key absent (None) → no enqueue.

    Default-off: if the key is missing from user_config the feature is disabled.
    Group B: star_paper uses CTE+RETURNING fetchrow (not _upsert_state_and_starred).
    """
    pool, conn = _make_pool_and_conn()

    # fetchrow[0]: CTE RETURNING
    conn.fetchrow.side_effect = [
        # assert_paper_ownership: discovered_by == caller → fast-grant (user_id=1).
        {"discovered_by": 1},
        {"is_new_row": True, "prev_starred": False},
    ]
    # fetchval[0] = COUNT(*) → 1 link; fetchval[1] = auto_push_on_star → None (key absent)
    conn.fetchval.side_effect = [1, None]

    mock_task, mock_enqueue = _mock_zotero_push_task()
    with patch.dict(task_registry._TASK_MAP, {"zotero.push": mock_task}):
        result = await papers.star_paper.__wrapped__(
            _mock_request(), paper_id=10, db_pool=pool, user_id=1
        )

    assert result == {"status": "ok", "paper_id": 10}
    mock_enqueue.assert_not_awaited()


# ---------------------------------------------------------------------------
# Scenario 5 — enqueue raises → handler still returns 200, logger.exception called
# (auto_push_on_star=True + project links → enqueue fails gracefully)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_star_enqueue_failure_is_best_effort():
    """If jobs_lib.enqueue raises, star_paper must return 200 and log via logger.exception.

    Group B: star_paper uses CTE+RETURNING fetchrow (not _upsert_state_and_starred).
    """
    pool, conn = _make_pool_and_conn()

    # fetchrow[0]: CTE RETURNING (new star → triggers enqueue)
    conn.fetchrow.side_effect = [
        # assert_paper_ownership: discovered_by == caller → fast-grant (user_id=1).
        {"discovered_by": 1},
        {"is_new_row": True, "prev_starred": False},
    ]
    # fetchval[0] = COUNT(*) → 1 link; fetchval[1] = auto_push_on_star → True
    conn.fetchval.side_effect = [1, True]

    mock_task = MagicMock()
    mock_task.defer_async = AsyncMock(side_effect=RuntimeError("queue down"))
    with (
        patch.dict(task_registry._TASK_MAP, {"zotero.push": mock_task}),
        patch.object(papers_lifecycle.logger, "exception") as mock_log_exc,
    ):
        # Must NOT raise
        result = await papers.star_paper.__wrapped__(
            _mock_request(), paper_id=10, db_pool=pool, user_id=1
        )

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
    transition guard (is_new_row=False AND prev_starred=True) skips the enqueue.
    This prevents duplicate Zotero pushes from client retries or double-tapped UI.
    Group B: transition is now detected via CTE RETURNING (is_new_row + prev_starred).
    """
    pool, conn = _make_pool_and_conn()

    # fetchrow[0]: CTE RETURNING (already starred: no transition)
    conn.fetchrow.side_effect = [
        # assert_paper_ownership: discovered_by == caller → fast-grant (user_id=1).
        {"discovered_by": 1},
        {"is_new_row": False, "prev_starred": True},  # existing row, was already starred
    ]
    # fetchval[0] = COUNT(*) → 1 link; fetchval[1] = auto_push_on_star → True
    conn.fetchval.side_effect = [1, True]

    mock_task, mock_enqueue = _mock_zotero_push_task()
    with patch.dict(task_registry._TASK_MAP, {"zotero.push": mock_task}):
        result = await papers.star_paper.__wrapped__(
            _mock_request(), paper_id=10, db_pool=pool, user_id=1
        )

    assert result == {"status": "ok", "paper_id": 10}
    mock_enqueue.assert_not_awaited()
