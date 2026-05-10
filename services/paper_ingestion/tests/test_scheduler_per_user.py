"""Sprint B / Wave-2 — scheduler wrappers fan out per user.

The four cron wrappers (``run_zotero_sync_wrapper``, ``run_pulse_wrapper``,
``run_pulse_classifier_training_wrapper``, ``run_weekly_digest_wrapper``)
must defer one job per active user — never a single ``user_id=None`` blob.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from paper_ingestion import scheduler


def _pool_with_users(user_ids: list[int]) -> tuple[MagicMock, AsyncMock]:
    """Mock pool whose first fetch returns the user list (then config rows)."""
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[{"id": uid} for uid in user_ids])
    conn.fetchrow = AsyncMock(return_value=None)
    pool = MagicMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=ctx)
    return pool, conn


@pytest.fixture()
def task_registry_mocks(monkeypatch):
    """Replace the global KIND_TO_TASK entries the wrappers defer through."""
    from jarvis_common import task_registry

    mock_tasks = {
        "pulse.generate": MagicMock(defer_async=AsyncMock()),
        "pulse.train_classifier": MagicMock(defer_async=AsyncMock()),
        "digest.weekly": MagicMock(defer_async=AsyncMock()),
        "zotero.sync_from_zotero": MagicMock(defer_async=AsyncMock()),
    }
    for kind, task in mock_tasks.items():
        monkeypatch.setitem(task_registry.KIND_TO_TASK, kind, task)
    return mock_tasks


@pytest.mark.asyncio
async def test_pulse_wrapper_defers_one_job_per_user(task_registry_mocks):
    pool, _conn = _pool_with_users([1, 2, 3])
    app = SimpleNamespace(state=SimpleNamespace(db_pool=pool))

    with patch.object(scheduler, "_is_pulse_enabled", AsyncMock(return_value=True)):
        await scheduler.run_pulse_wrapper(app)

    assert task_registry_mocks["pulse.generate"].defer_async.await_count == 3
    deferred_user_ids = sorted(
        call.kwargs["user_id"]
        for call in task_registry_mocks["pulse.generate"].defer_async.await_args_list
    )
    assert deferred_user_ids == [1, 2, 3]
    # No call should have user_id=None.
    assert all(uid is not None for uid in deferred_user_ids)


@pytest.mark.asyncio
async def test_pulse_classifier_wrapper_defers_one_job_per_user(task_registry_mocks):
    pool, _conn = _pool_with_users([10, 20])
    app = SimpleNamespace(state=SimpleNamespace(db_pool=pool))

    with patch.object(scheduler, "_is_pulse_enabled", AsyncMock(return_value=True)):
        await scheduler.run_pulse_classifier_training_wrapper(app)

    assert task_registry_mocks["pulse.train_classifier"].defer_async.await_count == 2


@pytest.mark.asyncio
async def test_zotero_wrapper_defers_one_job_per_user(task_registry_mocks):
    pool, _conn = _pool_with_users([7, 8])
    app = SimpleNamespace(state=SimpleNamespace(db_pool=pool))

    with patch.object(
        scheduler,
        "_get_zotero_poll_config",
        AsyncMock(return_value=(True, "0 * * * *")),
    ):
        await scheduler.run_zotero_sync_wrapper(app)

    assert task_registry_mocks["zotero.sync_from_zotero"].defer_async.await_count == 2


@pytest.mark.asyncio
async def test_weekly_digest_wrapper_defers_one_job_per_user(task_registry_mocks):
    pool, _conn = _pool_with_users([5])
    app = SimpleNamespace(state=SimpleNamespace(db_pool=pool))

    await scheduler.run_weekly_digest_wrapper(app)

    assert task_registry_mocks["digest.weekly"].defer_async.await_count == 1
    call = task_registry_mocks["digest.weekly"].defer_async.await_args
    assert call.kwargs["user_id"] == 5
    assert call.kwargs["days"] == 7


@pytest.mark.asyncio
async def test_no_active_users_results_in_zero_defers(task_registry_mocks):
    """Sprint B: empty users table → no defers (instead of legacy
    ``user_id=None`` system fallback)."""
    pool, _conn = _pool_with_users([])
    app = SimpleNamespace(state=SimpleNamespace(db_pool=pool))

    with patch.object(scheduler, "_is_pulse_enabled", AsyncMock(return_value=True)):
        await scheduler.run_pulse_wrapper(app)

    assert task_registry_mocks["pulse.generate"].defer_async.await_count == 0
