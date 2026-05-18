"""Smoke tests for Pulse scheduler wiring.

Ensures ``_is_pulse_enabled`` / ``_get_pulse_cron`` behave correctly and that
``run_pulse_wrapper`` is gated on the ``pulse.enabled`` flag.  Full scheduler
integration is exercised by ``test_scheduler_fixes``.
"""

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
async def test_run_pulse_wrapper_runs_when_enabled(scheduler_module):
    """Sprint B / Wave-2: ``run_pulse_wrapper`` fans out one defer per
    active user (no legacy ``user_id=None`` system fallback).
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
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = FakeRecord({"value": True})

    app = SimpleNamespace(
        state=SimpleNamespace(
            db_pool=pool,
            http_client=MagicMock(),
            embedder=MagicMock(),
        )
    )

    async def boom(**kwargs):
        raise RuntimeError("kaboom")

    import paper_ingestion.pulse.job as job_mod

    monkeypatch.setattr(job_mod, "run_pulse", boom)

    # Must not raise
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
    """Sprint B / Wave-2: pulse.train_classifier defers per active user."""
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
    """Sprint B / Wave-2: digest.weekly defers per active user with days=7."""
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
    finally:
        scheduler.shutdown(wait=False)
