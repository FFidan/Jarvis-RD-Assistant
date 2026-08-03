"""Scheduler wrappers fan out per user.

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
    # pulse de-dupe probe: every user's advisory lock is free
    conn.fetchval = AsyncMock(return_value=True)
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=conn)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=tx)
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
        monkeypatch.setitem(task_registry._TASK_MAP, kind, task)
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

    with (
        patch.object(
            scheduler,
            "_get_zotero_poll_config",
            AsyncMock(return_value=(True, "0 * * * *")),
        ),
        patch.object(
            scheduler,
            "_list_zotero_polling_users",
            AsyncMock(return_value=[7, 8]),
        ),
    ):
        await scheduler.run_zotero_sync_wrapper(app)

    assert task_registry_mocks["zotero.sync_from_zotero"].defer_async.await_count == 2


@pytest.mark.asyncio
async def test_zotero_wrapper_with_job_user_defers_only_that_user(task_registry_mocks):
    """Per-user APScheduler jobs must not fan out to every ready Zotero user."""
    pool, _conn = _pool_with_users([7, 8])
    app = SimpleNamespace(state=SimpleNamespace(db_pool=pool))

    with patch.object(
        scheduler,
        "_list_zotero_polling_users",
        AsyncMock(return_value=[7, 8]),
    ):
        await scheduler.run_zotero_sync_wrapper(app, user_id=8)

    defer = task_registry_mocks["zotero.sync_from_zotero"].defer_async
    defer.assert_awaited_once()
    assert defer.await_args.kwargs["user_id"] == 8


@pytest.mark.asyncio
async def test_zotero_wrapper_skips_unready_job_user(task_registry_mocks):
    """A stale per-user scheduled job should not enqueue after readiness is removed."""
    pool, _conn = _pool_with_users([7, 8])
    app = SimpleNamespace(state=SimpleNamespace(db_pool=pool))

    with patch.object(
        scheduler,
        "_list_zotero_polling_users",
        AsyncMock(return_value=[7]),
    ):
        await scheduler.run_zotero_sync_wrapper(app, user_id=8)

    task_registry_mocks["zotero.sync_from_zotero"].defer_async.assert_not_awaited()


@pytest.mark.asyncio
async def test_zotero_wrapper_uses_per_user_readiness_without_global_poll_row(
    task_registry_mocks,
):
    """A missing NULL-user poll_enabled row must not suppress ready personal configs."""
    pool, _conn = _pool_with_users([42])
    app = SimpleNamespace(state=SimpleNamespace(db_pool=pool))

    with (
        patch.object(
            scheduler,
            "_get_zotero_poll_config",
            AsyncMock(return_value=(False, "0 * * * *")),
        ),
        patch.object(
            scheduler,
            "_list_zotero_polling_users",
            AsyncMock(return_value=[42]),
        ),
    ):
        await scheduler.run_zotero_sync_wrapper(app)

    task_registry_mocks["zotero.sync_from_zotero"].defer_async.assert_awaited_once()
    assert (
        task_registry_mocks["zotero.sync_from_zotero"].defer_async.await_args.kwargs["user_id"]
        == 42
    )


@pytest.mark.asyncio
async def test_zotero_wrapper_skips_users_with_a_sync_already_running(task_registry_mocks):
    """A user whose sync lock is held gets no second job; the others still do."""
    pool, conn = _pool_with_users([7, 8])
    # probe order follows the user list: user 7 locked, user 8 free
    conn.fetchval = AsyncMock(side_effect=[False, True])
    app = SimpleNamespace(state=SimpleNamespace(db_pool=pool))

    with patch.object(
        scheduler,
        "_list_zotero_polling_users",
        AsyncMock(return_value=[7, 8]),
    ):
        await scheduler.run_zotero_sync_wrapper(app)

    defer = task_registry_mocks["zotero.sync_from_zotero"].defer_async
    defer.assert_awaited_once()
    assert defer.await_args.kwargs["user_id"] == 8


@pytest.mark.asyncio
async def test_zotero_sync_job_blocked_by_a_running_sync_never_polls_zotero():
    """The duplicate returns blocked without contacting Zotero at all."""
    from paper_ingestion.integrations import _zotero_jobs

    poll = AsyncMock()
    lock = MagicMock()
    lock.__aenter__ = AsyncMock(return_value=False)  # another sync holds it
    lock.__aexit__ = AsyncMock(return_value=None)
    ctx = MagicMock(update_progress=AsyncMock())

    with (
        patch.object(_zotero_jobs, "AdvisoryLock", MagicMock(return_value=lock)),
        patch.object(_zotero_jobs, "poll_zotero_library", poll),
    ):
        result = await _zotero_jobs._zotero_sync_from_zotero_job(
            pool=MagicMock(),
            http_client=MagicMock(),
            payload={"user_id": 7},
            ctx=ctx,
        )

    assert result["status"] == "blocked"
    poll.assert_not_awaited()


@pytest.mark.asyncio
async def test_zotero_sync_job_polls_when_it_wins_the_lock():
    """The uncontended run still polls and reports the poll's own status."""
    from paper_ingestion.integrations import _zotero_jobs

    poll = AsyncMock(return_value={"status": "ok", "imported": 3})
    lock = MagicMock()
    lock.__aenter__ = AsyncMock(return_value=True)
    lock.__aexit__ = AsyncMock(return_value=None)
    ctx = MagicMock(update_progress=AsyncMock())

    with (
        patch.object(_zotero_jobs, "AdvisoryLock", MagicMock(return_value=lock)),
        patch.object(_zotero_jobs, "poll_zotero_library", poll),
    ):
        result = await _zotero_jobs._zotero_sync_from_zotero_job(
            pool=MagicMock(),
            http_client=MagicMock(),
            payload={"user_id": 7},
            ctx=ctx,
        )

    assert result == {"status": "ok", "imported": 3}
    poll.assert_awaited_once()


@pytest.mark.asyncio
async def test_zotero_sync_job_locks_on_its_own_kind_and_user():
    """The job and the scheduler probe must agree on the key, or neither dedupes."""
    from jarvis_common.advisory_lock import _kind_lock_key
    from paper_ingestion.integrations import _zotero_jobs

    lock = MagicMock()
    lock.__aenter__ = AsyncMock(return_value=False)
    lock.__aexit__ = AsyncMock(return_value=None)
    lock_factory = MagicMock(return_value=lock)

    with (
        patch.object(_zotero_jobs, "AdvisoryLock", lock_factory),
        patch.object(_zotero_jobs, "poll_zotero_library", AsyncMock()),
    ):
        await _zotero_jobs._zotero_sync_from_zotero_job(
            pool=MagicMock(),
            http_client=MagicMock(),
            payload={"user_id": 7},
            ctx=MagicMock(update_progress=AsyncMock()),
        )

    assert lock_factory.call_args.kwargs["key1"] == _kind_lock_key("zotero.sync_from_zotero")
    assert lock_factory.call_args.kwargs["key2"] == 7


@pytest.mark.asyncio
async def test_list_zotero_polling_users_requires_user_credentials():
    """Scheduled Zotero polling should only fan out to users with ready personal config."""
    conn = AsyncMock()
    conn.fetch.return_value = [
        {"id": 1, "key": "zotero.poll_enabled", "value": True, "encrypted_value": None},
        {"id": 1, "key": "zotero.api_key", "value": None, "encrypted_value": b"cipher"},
        {"id": 1, "key": "zotero.user_id", "value": "123", "encrypted_value": None},
        {"id": 2, "key": "zotero.poll_enabled", "value": True, "encrypted_value": None},
        {"id": 2, "key": "zotero.user_id", "value": "456", "encrypted_value": None},
        {"id": 3, "key": "zotero.poll_enabled", "value": True, "encrypted_value": None},
        {"id": 3, "key": "zotero.api_key", "value": "key", "encrypted_value": None},
        {"id": 3, "key": "zotero.user_id", "value": "789", "encrypted_value": None},
        {"id": 3, "key": "zotero.library_type", "value": "group", "encrypted_value": None},
        {"id": 4, "key": "zotero.poll_enabled", "value": True, "encrypted_value": None},
        {"id": 4, "key": "zotero.api_key", "value": "key", "encrypted_value": None},
        {"id": 4, "key": "zotero.user_id", "value": "789", "encrypted_value": None},
        {"id": 4, "key": "zotero.library_type", "value": "group", "encrypted_value": None},
        {"id": 4, "key": "zotero.group_id", "value": "999", "encrypted_value": None},
    ]
    pool = MagicMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire.return_value = ctx

    assert await scheduler._list_zotero_polling_users(pool) == [1, 4]


@pytest.mark.asyncio
async def test_list_zotero_polling_schedules_uses_personal_cron_per_user():
    """Ready Zotero users keep distinct personal poll schedules."""
    conn = AsyncMock()
    conn.fetch.return_value = [
        {"id": 1, "key": "zotero.poll_enabled", "value": True, "encrypted_value": None},
        {"id": 1, "key": "zotero.api_key", "value": None, "encrypted_value": b"cipher"},
        {"id": 1, "key": "zotero.user_id", "value": "123", "encrypted_value": None},
        {"id": 1, "key": "zotero.poll_cron", "value": "0 6 * * *", "encrypted_value": None},
        {"id": 2, "key": "zotero.poll_enabled", "value": True, "encrypted_value": None},
        {"id": 2, "key": "zotero.api_key", "value": None, "encrypted_value": b"cipher"},
        {"id": 2, "key": "zotero.user_id", "value": "456", "encrypted_value": None},
    ]
    pool = MagicMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire.return_value = ctx

    assert await scheduler._list_zotero_polling_schedules(pool) == [
        (1, "0 6 * * *"),
        (2, "0 * * * *"),
    ]


@pytest.mark.asyncio
async def test_start_scheduler_registers_per_user_zotero_jobs():
    """Startup registers one Zotero poll job per ready user, not a global job."""
    pool = MagicMock()
    app = SimpleNamespace(state=SimpleNamespace(db_pool=pool))

    with (
        patch.object(scheduler, "_get_pulse_cron", AsyncMock(return_value="0 4 * * *")),
        patch.object(
            scheduler,
            "_list_zotero_polling_schedules",
            AsyncMock(return_value=[(7, "0 6 * * *"), (8, "0 8 * * *")]),
        ),
        patch("paper_ingestion.scheduler.refresh_recommendations", new=AsyncMock(return_value=0)),
    ):
        started = await scheduler.start_scheduler(app, interval_hours=0)

    try:
        assert started.get_job("zotero_library_sync") is None
        assert started.get_job("zotero_library_sync_7") is not None
        assert started.get_job("zotero_library_sync_8") is not None
    finally:
        started.shutdown(wait=False)


@pytest.mark.asyncio
async def test_reconcile_zotero_poll_job_adds_and_removes_user_job():
    """Live settings writes reconcile only the caller's scheduler job."""
    fake_scheduler = MagicMock()
    fake_scheduler.get_job.return_value = object()
    pool = MagicMock()
    app = SimpleNamespace(state=SimpleNamespace(db_pool=pool))

    with patch.object(
        scheduler,
        "_list_zotero_polling_schedules",
        AsyncMock(side_effect=[[(7, "0 6 * * *")], []]),
    ):
        await scheduler.reconcile_zotero_poll_job(
            scheduler=fake_scheduler,
            app=app,
            db_pool=pool,
            user_id=7,
        )
        await scheduler.reconcile_zotero_poll_job(
            scheduler=fake_scheduler,
            app=app,
            db_pool=pool,
            user_id=7,
        )

    fake_scheduler.add_job.assert_called_once()
    assert fake_scheduler.add_job.call_args.kwargs["id"] == "zotero_library_sync_7"
    assert fake_scheduler.add_job.call_args.kwargs["args"] == [app, 7]
    fake_scheduler.remove_job.assert_called_once_with("zotero_library_sync_7")


@pytest.mark.asyncio
async def test_reconcile_zotero_poll_job_keeps_job_when_read_fails():
    """Transient config-read failures must not be interpreted as readiness loss."""
    fake_scheduler = MagicMock()
    fake_scheduler.get_job.return_value = object()
    pool = MagicMock()
    app = SimpleNamespace(state=SimpleNamespace(db_pool=pool))

    with patch.object(
        scheduler,
        "_fetch_zotero_poll_config_rows",
        AsyncMock(side_effect=RuntimeError("db unavailable")),
    ):
        with pytest.raises(RuntimeError, match="db unavailable"):
            await scheduler.reconcile_zotero_poll_job(
                scheduler=fake_scheduler,
                app=app,
                db_pool=pool,
                user_id=7,
            )

    fake_scheduler.remove_job.assert_not_called()


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
    """Empty users table → no defers (instead of legacy
    ``user_id=None`` system fallback)."""
    pool, _conn = _pool_with_users([])
    app = SimpleNamespace(state=SimpleNamespace(db_pool=pool))

    with patch.object(scheduler, "_is_pulse_enabled", AsyncMock(return_value=True)):
        await scheduler.run_pulse_wrapper(app)

    assert task_registry_mocks["pulse.generate"].defer_async.await_count == 0


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, True),
        (False, False),
        (None, False),
        ("true", True),
        ("false", False),
        ("yes", True),
        ("no", False),
        ("1", True),
        ("0", False),
        ("TRUE", True),
        ("False", False),
    ],
)
def test_coerce_bool_parity(value, expected):
    """Characterize the shared bool coercion the zotero-poll gate relies on."""
    assert scheduler._coerce_bool(value) is expected
