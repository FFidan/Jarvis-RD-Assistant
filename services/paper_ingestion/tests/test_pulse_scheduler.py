"""Smoke tests for Pulse scheduler wiring.

Ensures ``_is_pulse_enabled`` / ``_get_pulse_cron`` behave correctly and that
``run_pulse_wrapper`` is gated on the ``pulse.enabled`` flag.  Full scheduler
integration is exercised by ``test_scheduler_fixes``.
"""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from tests.conftest import FakeRecord, _make_pool_and_conn


@pytest.fixture
def scheduler_module():
    import paper_ingestion.scheduler as scheduler

    return scheduler


@pytest.mark.asyncio
async def test_is_pulse_enabled_false_when_missing(scheduler_module):
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = None
    assert await scheduler_module._is_pulse_enabled(pool) is False


@pytest.mark.asyncio
async def test_is_pulse_enabled_true_when_bool(scheduler_module):
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = FakeRecord({"value": True})
    assert await scheduler_module._is_pulse_enabled(pool) is True


@pytest.mark.asyncio
async def test_is_pulse_enabled_false_on_db_error(scheduler_module):
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.side_effect = RuntimeError("db down")
    assert await scheduler_module._is_pulse_enabled(pool) is False


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        (True, True),
        (False, False),
        ("true", True),
        ("yes", True),
        ("1", True),
        ("false", False),
        ("no", False),
        ("null", False),
        ("", False),
        (None, False),
        # Fail-closed: strings outside the recognised vocabulary must NOT enable
        # the flag. bool("maybe") is True, so a truthiness fallback here would
        # silently switch nightly Pulse on for a value we cannot interpret.
        ("maybe", False),
        ("enabled", False),
        ("TRUE ", False),
    ],
)
@pytest.mark.asyncio
async def test_is_pulse_enabled_coerces_stored_values(scheduler_module, stored, expected):
    """Pins the shared flag reader's coercion, including its fail-closed handling
    of unrecognised strings — the assertion that catches a fail-open regression."""
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = FakeRecord({"value": stored})
    assert await scheduler_module._is_pulse_enabled(pool) is expected


@pytest.mark.asyncio
async def test_get_pulse_cron_default(scheduler_module):
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = None
    cron = await scheduler_module._get_pulse_cron(pool)
    assert cron == "0 4 * * *"


@pytest.mark.asyncio
async def test_get_pulse_cron_custom(scheduler_module):
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = FakeRecord({"value": "15 3 * * *"})
    cron = await scheduler_module._get_pulse_cron(pool)
    assert cron == "15 3 * * *"


@pytest.mark.asyncio
async def test_run_pulse_wrapper_skips_when_disabled(scheduler_module):
    """When pulse.enabled is False, run_pulse_wrapper must not enqueue any job."""
    import jarvis_common.task_registry as task_registry
    from procrastinate import testing

    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = FakeRecord({"value": False})

    app = SimpleNamespace(
        state=SimpleNamespace(
            db_pool=pool,
            http_client=MagicMock(),
            embedder=MagicMock(),
        )
    )

    in_memory = testing.InMemoryConnector()
    with task_registry.app.replace_connector(in_memory):
        await scheduler_module.run_pulse_wrapper(app)

    assert len(in_memory.jobs) == 0


@pytest.mark.asyncio
async def test_run_pulse_wrapper_skips_under_maintenance(scheduler_module, tmp_path, monkeypatch):
    """A fresh maintenance sentinel makes run_pulse_wrapper return before any DB read/defer."""
    import jarvis_common.task_registry as task_registry
    from procrastinate import testing

    monkeypatch.setenv("MAINTENANCE_SENTINEL", str(tmp_path / ".maintenance"))
    monkeypatch.setenv("MAINTENANCE_DESTRUCTIVE_SENTINEL", str(tmp_path / ".destructive"))
    (tmp_path / ".maintenance").touch()

    pool, conn = _make_pool_and_conn()
    app = SimpleNamespace(state=SimpleNamespace(db_pool=pool))

    in_memory = testing.InMemoryConnector()
    with task_registry.app.replace_connector(in_memory):
        await scheduler_module.run_pulse_wrapper(app)

    assert len(in_memory.jobs) == 0  # nothing deferred
    conn.fetchrow.assert_not_called()  # returned before reading pulse.enabled


@pytest.mark.asyncio
async def test_run_pulse_wrapper_runs_when_enabled(scheduler_module):
    """``run_pulse_wrapper`` fans out one defer per active user
    (no legacy ``user_id=None`` system fallback).
    """
    import jarvis_common.task_registry as task_registry

    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = FakeRecord({"value": True})
    # _list_active_users uses conn.fetch — return one user.
    conn.fetch.return_value = [{"id": 1}]
    app = SimpleNamespace(state=SimpleNamespace(db_pool=pool))

    mock_pulse_task = MagicMock()
    mock_pulse_defer = AsyncMock()
    mock_pulse_task.defer_async = mock_pulse_defer

    with patch.dict(task_registry._TASK_MAP, {"pulse.generate": mock_pulse_task}):
        await scheduler_module.run_pulse_wrapper(app)

    mock_pulse_defer.assert_awaited_once()
    assert mock_pulse_defer.await_args is not None
    call_kwargs = mock_pulse_defer.await_args.kwargs
    assert call_kwargs["user_id"] == 1
    assert isinstance(call_kwargs["job_id"], str)
    assert len(call_kwargs["job_id"]) == 36  # uuid4 string form


@pytest.mark.asyncio
async def test_run_pulse_wrapper_swallows_errors(scheduler_module, monkeypatch):
    """run_pulse_wrapper must not propagate exceptions from _defer_per_user.

    run_pulse_wrapper calls _defer_per_user (which looks up the task in
    task_registry._TASK_MAP and calls defer_async).  We make that call raise
    to verify the outer try/except swallows the error instead of crashing
    the APScheduler worker thread.
    """
    import jarvis_common.task_registry as task_registry

    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = FakeRecord({"value": True})
    conn.fetch.return_value = [{"id": 1}]  # one active user

    app = SimpleNamespace(state=SimpleNamespace(db_pool=pool))

    boom_task = MagicMock()
    boom_task.defer_async = AsyncMock(side_effect=RuntimeError("kaboom"))

    with patch.dict(task_registry._TASK_MAP, {"pulse.generate": boom_task}):
        # Must not raise even though defer_async blows up
        await scheduler_module.run_pulse_wrapper(app)


@pytest.mark.asyncio
async def test_run_pulse_classifier_training_wrapper_skips_when_disabled(scheduler_module):
    import jarvis_common.task_registry as task_registry
    from procrastinate import testing

    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = FakeRecord({"value": False})
    app = SimpleNamespace(state=SimpleNamespace(db_pool=pool))

    in_memory = testing.InMemoryConnector()
    with task_registry.app.replace_connector(in_memory):
        await scheduler_module.run_pulse_classifier_training_wrapper(app)

    assert len(in_memory.jobs) == 0


@pytest.mark.asyncio
async def test_run_pulse_classifier_training_wrapper_defers_when_enabled(scheduler_module):
    """pulse.train_classifier defers per active user."""
    import jarvis_common.task_registry as task_registry

    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = FakeRecord({"value": True})
    conn.fetch.return_value = [{"id": 99}]
    app = SimpleNamespace(state=SimpleNamespace(db_pool=pool))

    mock_train_task = MagicMock()
    mock_train_defer = AsyncMock()
    mock_train_task.defer_async = mock_train_defer

    with patch.dict(task_registry._TASK_MAP, {"pulse.train_classifier": mock_train_task}):
        await scheduler_module.run_pulse_classifier_training_wrapper(app)

    mock_train_defer.assert_awaited_once()
    assert mock_train_defer.await_args is not None
    call_kwargs = mock_train_defer.await_args.kwargs
    assert call_kwargs["user_id"] == 99
    assert isinstance(call_kwargs["job_id"], str)
    assert len(call_kwargs["job_id"]) == 36  # uuid4 string form


@pytest.mark.asyncio
async def test_run_weekly_digest_wrapper_enqueues_digest_weekly(scheduler_module):
    """digest.weekly defers per active user with days=7."""
    import jarvis_common.task_registry as task_registry

    pool, conn = _make_pool_and_conn()
    conn.fetch.return_value = [{"id": 7}]
    app = SimpleNamespace(state=SimpleNamespace(db_pool=pool))

    mock_digest_task = MagicMock()
    mock_digest_defer = AsyncMock()
    mock_digest_task.defer_async = mock_digest_defer

    with patch.dict(task_registry._TASK_MAP, {"digest.weekly": mock_digest_task}):
        await scheduler_module.run_weekly_digest_wrapper(app)

    mock_digest_defer.assert_awaited_once()
    assert mock_digest_defer.await_args is not None
    call_kwargs = mock_digest_defer.await_args.kwargs
    assert call_kwargs["days"] == 7
    assert call_kwargs["user_id"] == 7
    # Critical SSE-bridge contract: JARVIS UUID must travel as args.job_id.
    assert isinstance(call_kwargs["job_id"], str)
    assert len(call_kwargs["job_id"]) == 36  # uuid4 string form


@pytest.mark.asyncio
async def test_start_scheduler_registers_classifier_and_weekly_jobs(scheduler_module):
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = FakeRecord({"value": "0 4 * * *"})
    conn.fetch.return_value = []
    app = SimpleNamespace(state=SimpleNamespace(db_pool=pool))

    with patch("paper_ingestion.scheduler.refresh_recommendations", new=AsyncMock(return_value=0)):
        scheduler = await scheduler_module.start_scheduler(app, interval_hours=0)

    try:
        assert scheduler.get_job("pulse_classifier_training") is not None
        assert scheduler.get_job("weekly_digest") is not None
        assert scheduler.get_job("purge_magic_link_tokens") is not None
    finally:
        scheduler.shutdown(wait=False)


@pytest.mark.asyncio
async def test_users_without_active_pulse_lock_filters_locked_users(scheduler_module):
    """_users_without_active_pulse_lock returns only users whose lock is free.

    User A (id=1): pg_try_advisory_xact_lock returns False — lock held.
    User B (id=2): pg_try_advisory_xact_lock returns True  — lock free.
    Expected: only [2] is returned; no explicit unlock is called (xact-lock
    auto-releases at transaction end).
    """
    from unittest.mock import AsyncMock, MagicMock

    conn = AsyncMock()
    # First fetchval call → user 1 → lock held (False)
    # Second fetchval call → user 2 → lock free (True)
    conn.fetchval = AsyncMock(side_effect=[False, True])

    # conn.transaction() must return an async context manager (xact-lock branch).
    txn_cm = MagicMock()
    txn_cm.__aenter__ = AsyncMock(return_value=txn_cm)
    txn_cm.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn_cm)

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx

    result = await scheduler_module._users_without_active_pulse_lock(pool, [1, 2])

    assert result == [2]
    # xact-lock auto-releases — no explicit pg_advisory_unlock call.
    assert conn.transaction.call_count == 2  # one transaction per user


@pytest.mark.asyncio
async def test_run_pulse_wrapper_skips_locked_users(scheduler_module):
    """run_pulse_wrapper must pass only unlocked users to _defer_per_user.

    Two active users; user A's lock is held → only user B gets a deferred job.
    conn.transaction() is already wired by _make_pool_and_conn (with_transaction=True).
    """
    import jarvis_common.task_registry as task_registry
    from unittest.mock import AsyncMock, patch

    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = FakeRecord({"value": True})  # pulse enabled
    conn.fetch.return_value = [{"id": 1}, {"id": 2}]  # two active users

    # fetchval: user 1 → locked (False), user 2 → free (True)
    conn.fetchval = AsyncMock(side_effect=[False, True])

    app = __import__("types").SimpleNamespace(
        state=__import__("types").SimpleNamespace(db_pool=pool)
    )

    mock_pulse_task = MagicMock()
    mock_pulse_defer = AsyncMock()
    mock_pulse_task.defer_async = mock_pulse_defer

    with patch.dict(task_registry._TASK_MAP, {"pulse.generate": mock_pulse_task}):
        await scheduler_module.run_pulse_wrapper(app)

    # Only one defer — for user 2 (user 1 was locked)
    mock_pulse_defer.assert_awaited_once()
    call_kwargs = mock_pulse_defer.await_args.kwargs
    assert call_kwargs["user_id"] == 2


@pytest.mark.asyncio
async def test_run_pulse_wrapper_all_locked_skips_entirely(scheduler_module, caplog):
    """run_pulse_wrapper must not call _defer_per_user when all users are locked.

    conn.transaction() is already wired by _make_pool_and_conn (with_transaction=True).
    """
    import jarvis_common.task_registry as task_registry
    from unittest.mock import AsyncMock, patch

    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = FakeRecord({"value": True})  # pulse enabled
    conn.fetch.return_value = [{"id": 1}]  # one active user, locked

    conn.fetchval = AsyncMock(return_value=False)  # lock held for every user

    app = __import__("types").SimpleNamespace(
        state=__import__("types").SimpleNamespace(db_pool=pool)
    )

    mock_pulse_task = MagicMock()
    mock_pulse_defer = AsyncMock()
    mock_pulse_task.defer_async = mock_pulse_defer

    with patch.dict(task_registry._TASK_MAP, {"pulse.generate": mock_pulse_task}):
        with caplog.at_level(logging.INFO, logger="paper_ingestion.scheduler"):
            await scheduler_module.run_pulse_wrapper(app)

    mock_pulse_defer.assert_not_awaited()
    assert any("all active users have an in-flight run" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_run_pulse_wrapper_zero_active_users_uses_distinct_log(scheduler_module, caplog):
    """run_pulse_wrapper must not call _defer_per_user when there are zero active users,
    and the skip log must be distinguishable from the all-locked message."""
    import jarvis_common.task_registry as task_registry
    from unittest.mock import AsyncMock, patch

    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = FakeRecord({"value": True})  # pulse enabled
    conn.fetch.return_value = []  # zero active users

    app = __import__("types").SimpleNamespace(
        state=__import__("types").SimpleNamespace(db_pool=pool)
    )

    mock_pulse_task = MagicMock()
    mock_pulse_defer = AsyncMock()
    mock_pulse_task.defer_async = mock_pulse_defer

    with patch.dict(task_registry._TASK_MAP, {"pulse.generate": mock_pulse_task}):
        with caplog.at_level(logging.INFO, logger="paper_ingestion.scheduler"):
            await scheduler_module.run_pulse_wrapper(app)

    mock_pulse_defer.assert_not_awaited()
    assert not any("in-flight run" in r.message for r in caplog.records), (
        "zero active users must not be reported as an in-flight-run skip"
    )
    assert any("no active users" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_apply_pulse_cron_rollback_also_fails_still_raises_http_400(caplog):
    """Outer HTTPException must fire even if the inner rollback raises.

    Pinned regression: previous code used contextlib.suppress(Exception) and
    silently lost the secondary failure; we now log it at WARNING.
    """
    from fastapi import HTTPException

    from paper_ingestion.services.scheduler_effects import apply_pulse_cron

    call_count = 0

    def _reschedule_job(job_id: str, *, trigger: object) -> None:
        del job_id, trigger  # callback signature only; values unused in this stub
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            raise RuntimeError("rollback scheduler also broken")

    fake_job = SimpleNamespace(next_run_time=None)
    stub_scheduler = SimpleNamespace(
        reschedule_job=_reschedule_job,
        get_job=lambda job_id: (job_id, fake_job)[1],
    )

    pool, _ = _make_pool_and_conn()

    with caplog.at_level(logging.WARNING, logger="paper_ingestion.services.scheduler_effects"):
        with pytest.raises(HTTPException) as exc_info:
            await apply_pulse_cron(
                db_pool=pool,
                scheduler=stub_scheduler,
                new_cron="0 3 * * *",
                old_cron="0 4 * * *",
            )

    assert exc_info.value.status_code == 400
    assert any(
        "scheduler revert also failed" in record.message and record.levelno == logging.WARNING
        for record in caplog.records
    )
