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
async def test_run_pulse_wrapper_skips_when_disabled(scheduler_module, monkeypatch):
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = FakeRecord({"value": False})

    app = SimpleNamespace(
        state=SimpleNamespace(
            db_pool=pool,
            http_client=MagicMock(),
            embedder=MagicMock(),
        )
    )

    called = {"n": 0}

    async def fake_run_pulse(**kwargs):
        called["n"] += 1
        return {}

    # Ensure app.pulse.job exists and its run_pulse is the one we stub
    import paper_ingestion.pulse.job as job_mod

    monkeypatch.setattr(job_mod, "run_pulse", fake_run_pulse)

    await scheduler_module.run_pulse_wrapper(app)
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_run_pulse_wrapper_runs_when_enabled(scheduler_module, monkeypatch):
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = FakeRecord({"value": True})

    app = SimpleNamespace(
        state=SimpleNamespace(
            db_pool=pool,
            http_client=MagicMock(),
            embedder=MagicMock(),
        )
    )

    called = {"n": 0}

    async def fake_run_pulse(**kwargs):
        called["n"] += 1
        return {"duration_s": 1.0}

    import paper_ingestion.pulse.job as job_mod

    monkeypatch.setattr(job_mod, "run_pulse", fake_run_pulse)

    await scheduler_module.run_pulse_wrapper(app)
    assert called["n"] == 1


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
    """B.4 Step 3: pulse.train_classifier now defers via procrastinate.

    Asserts the in-memory connector recorded a deferred job whose args carry
    the JARVIS UUID under ``job_id`` and ``user_id=None`` (system call).
    """
    import jarvis_common.task_registry as task_registry
    from procrastinate import testing

    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = FakeRecord({"value": True})
    app = SimpleNamespace(state=SimpleNamespace(db_pool=pool))

    in_memory = testing.InMemoryConnector()
    with task_registry.app.replace_connector(in_memory):
        await scheduler_module.run_pulse_classifier_training_wrapper(app)

    jobs = in_memory.jobs
    assert len(jobs) == 1
    deferred = next(iter(jobs.values()))
    assert deferred["task_name"] == "pulse.train_classifier"
    assert deferred["queue_name"] == "paper_ingestion"
    args = deferred["args"]
    assert args["user_id"] is None
    assert isinstance(args["job_id"], str)
    assert len(args["job_id"]) == 36  # uuid4 string form


@pytest.mark.asyncio
async def test_run_weekly_digest_wrapper_enqueues_digest_weekly(scheduler_module):
    """B.4 Step 3 canary: digest.weekly now defers via procrastinate.

    Asserts the in-memory connector recorded a deferred job whose args carry
    the JARVIS UUID under ``job_id`` (the SSE bridge keys on this) and the
    ``days=7`` payload from the scheduler wrapper.
    """
    import jarvis_common.task_registry as task_registry
    from procrastinate import testing

    pool, _conn = _make_pool_and_conn()
    app = SimpleNamespace(state=SimpleNamespace(db_pool=pool))

    in_memory = testing.InMemoryConnector()
    with task_registry.app.replace_connector(in_memory):
        await scheduler_module.run_weekly_digest_wrapper(app)

    jobs = in_memory.jobs
    assert len(jobs) == 1
    deferred = next(iter(jobs.values()))
    assert deferred["task_name"] == "digest.weekly"
    assert deferred["queue_name"] == "paper_ingestion"
    args = deferred["args"]
    assert args["days"] == 7
    # Critical SSE-bridge contract: JARVIS UUID must travel as args.job_id.
    assert isinstance(args["job_id"], str)
    assert len(args["job_id"]) == 36  # uuid4 string form


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
