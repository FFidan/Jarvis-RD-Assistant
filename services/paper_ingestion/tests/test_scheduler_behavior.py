"""Tests for scheduler.py bug fixes (http_client arg, path traversal guard)."""

import logging
import sys
from datetime import UTC, datetime, timedelta
from math import ceil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from apscheduler.triggers.interval import IntervalTrigger

# ---------------------------------------------------------------------------
# Stub objects — created at module scope so tests can mutate their attributes,
# but NOT installed into sys.modules here (that would pollute collection).
# ---------------------------------------------------------------------------

# Import the real path-resolution guard so the stubbed pdf_processor module
# exposes genuine traversal semantics (a bare MagicMock attribute would always
# be truthy and silently disable the guard).
from paper_ingestion.pdf_processor import resolve_safe_pdf_path as _real_resolve_safe_pdf_path

_pdf_proc_stub = MagicMock()
_pdf_proc_stub.PDF_STORAGE_PATH = "/data/pdfs"
_pdf_proc_stub.resolve_safe_pdf_path = _real_resolve_safe_pdf_path

_main_stub = MagicMock()
_workflow_stub = MagicMock()

# ---------------------------------------------------------------------------
# Autouse fixture: install internal-module stubs + re-import stubbed module each test
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _install_stubs(monkeypatch):
    """Install project-module stubs into sys.modules for the duration of each test.

    monkeypatch.setitem auto-reverses on teardown, so sys.modules stays clean
    for test files collected/run after this one.
    """
    # Reset shared stubs so mutations from previous tests don't bleed through.
    _pdf_proc_stub.reset_mock()
    _pdf_proc_stub.PDF_STORAGE_PATH = "/data/pdfs"
    _pdf_proc_stub.resolve_safe_pdf_path = _real_resolve_safe_pdf_path
    _main_stub.reset_mock()
    _workflow_stub.reset_mock()
    _workflow_stub.upsert_paper = AsyncMock()
    _workflow_stub.run_process_pdf = AsyncMock()

    # Keep a reference under _main_stub for assertion helpers.
    _main_stub.upsert_paper = _workflow_stub.upsert_paper
    _main_stub.run_process_pdf = _workflow_stub.run_process_pdf

    monkeypatch.setitem(sys.modules, "paper_ingestion.pdf_processor", _pdf_proc_stub)
    monkeypatch.setitem(sys.modules, "paper_ingestion.main", _main_stub)
    monkeypatch.setitem(sys.modules, "paper_ingestion.services", MagicMock())
    monkeypatch.setitem(sys.modules, "paper_ingestion.services.pdf_workflow", _workflow_stub)
    # Force re-import of scheduler + auto_fetch so they resolve against the freshly installed stubs.
    monkeypatch.delitem(sys.modules, "paper_ingestion.scheduler", raising=False)
    monkeypatch.delitem(sys.modules, "paper_ingestion.pipelines.auto_fetch", raising=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app_state(
    *,
    sources_rows: list | None = None,
    topics_rows: list | None = None,
    to_download: list | None = None,
    to_process: list | None = None,
) -> SimpleNamespace:
    """Build a fake ``app`` object that mirrors FastAPI app.state.

    Parameters
    ----------
    sources_rows : list | None
        Rows returned for the ``paper_sources`` query.
    topics_rows : list | None
        Rows returned for the ``topics`` query.
    to_download : list | None
        Rows returned for the download query.
    to_process : list | None
        Rows returned for the process query.

    Returns
    -------
    SimpleNamespace
        A minimal ``app`` stand-in with ``app.state.*`` attributes.
    """
    if sources_rows is None:
        sources_rows = []
    if topics_rows is None:
        topics_rows = []
    if to_download is None:
        to_download = []
    if to_process is None:
        to_process = []

    # Each call to conn.fetch returns the next result set in order.
    fetch_results = iter([sources_rows, topics_rows, to_download, to_process])

    conn = AsyncMock()
    conn.fetch = AsyncMock(side_effect=lambda *_a, **_kw: next(fetch_results))

    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    http_client = MagicMock(name="http_client")
    pdf_processor = MagicMock(name="pdf_processor")
    embedder = MagicMock(name="embedder")

    app = SimpleNamespace(
        state=SimpleNamespace(
            db_pool=pool,
            http_client=http_client,
            pdf_processor=pdf_processor,
            embedder=embedder,
        ),
    )
    return app


# ---------------------------------------------------------------------------
# source is instantiated with http_client
# ---------------------------------------------------------------------------


async def test_source_instantiated_with_http_client() -> None:
    """Source class must be called with (config, http_client), not just (config)."""
    from paper_ingestion.scheduler import run_auto_pipeline  # noqa: PLC0415

    fake_source_instance = AsyncMock()
    fake_source_instance.search = AsyncMock(return_value=[])

    fake_source_class = MagicMock(return_value=fake_source_instance)

    source_row = {
        "id": 1,
        "source_type": "arxiv",
        "enabled": True,
        "config": {},
    }
    topic_row = {"name": "transformers"}

    app = _make_app_state(
        sources_rows=[source_row],
        topics_rows=[topic_row],
    )

    with (
        patch.dict("os.environ", {"AUTO_FETCH_INTERVAL_HOURS": "1"}),
        patch(
            "paper_ingestion.pipelines.auto_fetch.get_source_class",
            return_value=fake_source_class,
        ),
    ):
        await run_auto_pipeline(app)

    # The source class must have been called with two positional args
    fake_source_class.assert_called_once()
    call_args = fake_source_class.call_args
    assert len(call_args.args) == 2, (
        f"Expected 2 positional args (config, http_client), got {len(call_args.args)}"
    )
    assert call_args.args[1] is app.state.http_client


# ---------------------------------------------------------------------------
# path traversal is rejected
# ---------------------------------------------------------------------------


async def test_path_traversal_pdf_skipped(caplog: pytest.LogCaptureFixture) -> None:
    """Papers whose pdf_local_path escapes the storage dir must be skipped."""
    from paper_ingestion.scheduler import run_auto_pipeline  # noqa: PLC0415

    traversal_row = {
        "id": 42,
        "pdf_local_path": "/data/pdfs/../../etc/passwd",
    }

    app = _make_app_state(to_process=[traversal_row])

    # Reset the shared mock so we can assert it was NOT called
    _main_stub.run_process_pdf.reset_mock()

    with (
        patch.dict("os.environ", {"AUTO_FETCH_INTERVAL_HOURS": "1"}),
        caplog.at_level(logging.WARNING, logger="paper_ingestion.pipelines.auto_fetch"),
    ):
        await run_auto_pipeline(app)

    # _run_process_pdf must NOT have been called for the traversal path
    _main_stub.run_process_pdf.assert_not_called()

    # The warning must have been logged
    assert any(
        "Skipping paper 42" in msg and "outside storage dir" in msg for msg in caplog.messages
    ), f"Expected traversal warning, got: {caplog.messages}"


async def test_valid_pdf_path_is_processed() -> None:
    """Papers with a pdf_local_path inside the storage dir should be processed."""
    from paper_ingestion.scheduler import run_auto_pipeline  # noqa: PLC0415

    valid_row = {
        "id": 7,
        "pdf_local_path": "/data/pdfs/paper_7.pdf",
    }

    app = _make_app_state(to_process=[valid_row])

    # Reset the shared mock
    _main_stub.run_process_pdf.reset_mock()

    with patch.dict("os.environ", {"AUTO_FETCH_INTERVAL_HOURS": "1"}):
        await run_auto_pipeline(app)

    # _run_process_pdf should have been called for the valid path
    _main_stub.run_process_pdf.assert_called_once()
    call_args = _main_stub.run_process_pdf.call_args
    assert call_args.args[0] == 7
    assert call_args.args[1] == Path("/data/pdfs/paper_7.pdf")


# ---------------------------------------------------------------------------
# H14/H15: scheduler always starts; auto_pipeline self-gates on zero interval
# ---------------------------------------------------------------------------


async def test_scheduler_always_starts() -> None:
    """start_scheduler must return a scheduler even when interval=0 and pulse is disabled.

    Previously, the lifespan hook gated ``start_scheduler`` on interval > 0
    or pulse enabled.  With the fix, the scheduler is always started so that
    live-toggles (Settings UI) take effect without a restart.
    """
    from paper_ingestion.scheduler import start_scheduler  # noqa: PLC0415

    # Minimal app with a db_pool that returns a cron value for _get_pulse_cron
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"value": '"0 2 * * *"'})
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    fake_app = SimpleNamespace(
        state=SimpleNamespace(
            db_pool=pool,
        )
    )

    with patch("paper_ingestion.scheduler.refresh_recommendations", new=AsyncMock(return_value=0)):
        scheduler = await start_scheduler(fake_app, interval_hours=0)

    try:
        assert scheduler is not None, "scheduler must not be None when interval=0"
        # auto_pipeline job must be registered (self-gated, not absent)
        job = scheduler.get_job("auto_pipeline")
        assert job is not None, "auto_pipeline job must be registered even when interval=0"
    finally:
        scheduler.shutdown(wait=False)


async def test_scheduler_honors_fractional_auto_fetch_interval() -> None:
    """start_scheduler must not truncate a fractional interval_hours to an int.

    Previously ``max(int(interval_hours), 1)`` truncated e.g. 1.5h to 1h,
    silently doubling the effective poll frequency. IntervalTrigger accepts a
    float, so the fix drops the ``int()`` cast entirely.
    """
    from paper_ingestion.scheduler import start_scheduler  # noqa: PLC0415

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"value": '"0 2 * * *"'})
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    fake_app = SimpleNamespace(state=SimpleNamespace(db_pool=pool))

    with patch("paper_ingestion.scheduler.refresh_recommendations", new=AsyncMock(return_value=0)):
        scheduler = await start_scheduler(fake_app, interval_hours=1.5)

    try:
        job = scheduler.get_job("auto_pipeline")
        assert job is not None
        assert job.trigger.interval == timedelta(hours=1.5), (
            f"Expected a 1.5h interval, got {job.trigger.interval}"
        )
    finally:
        scheduler.shutdown(wait=False)


async def test_auto_pipeline_self_gates_on_zero_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    """run_auto_pipeline must return early without touching the DB when interval=0."""
    from paper_ingestion.scheduler import run_auto_pipeline  # noqa: PLC0415

    monkeypatch.setenv("AUTO_FETCH_INTERVAL_HOURS", "0")

    # Track whether the DB pool was accessed
    pool_acquire_called = False

    class _TrackingPool:
        def acquire(self):
            nonlocal pool_acquire_called
            pool_acquire_called = True
            raise AssertionError("DB pool must not be acquired when interval=0")

    fake_app = SimpleNamespace(
        state=SimpleNamespace(
            db_pool=_TrackingPool(),
        )
    )

    # Should return immediately without any DB calls
    await run_auto_pipeline(fake_app)

    assert not pool_acquire_called, "DB pool must not be acquired when interval_hours=0"


# ---------------------------------------------------------------------------
# Periodic work survives a restart: anchored intervals, misfire grace, catch-up
# ---------------------------------------------------------------------------


def _make_scheduler_app(*, last_run: str | None = None) -> SimpleNamespace:
    """Fake app whose stubbed DB answers both reads ``start_scheduler`` makes.

    ``last_run`` is the stored auto-fetch last-run stamp; ``None`` means the row
    does not exist yet.
    """

    async def _fetchrow(query: str, *_args):
        if "pulse.cron" in query:
            return {"value": "0 2 * * *"}
        return None if last_run is None else {"value": last_run}

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=_fetchrow)
    conn.fetch = AsyncMock(return_value=[])
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return SimpleNamespace(state=SimpleNamespace(db_pool=pool))


async def _start(app: SimpleNamespace, interval_hours: float):
    """Start the scheduler with recommendation refresh stubbed out."""
    from paper_ingestion.scheduler import start_scheduler  # noqa: PLC0415

    with patch("paper_ingestion.scheduler.refresh_recommendations", new=AsyncMock(return_value=0)):
        return await start_scheduler(app, interval_hours=interval_hours)


def _next_grid_point(epoch: datetime, interval_hours: float, now: datetime) -> datetime:
    """First epoch-anchored interval fire at or after ``now``.

    Mirrors the arithmetic an interval trigger applies to a start date already in
    the past, so an untouched job's next fire is predictable at any wall-clock
    time instead of only outside a few minutes around a grid point.
    """
    interval = timedelta(hours=interval_hours)
    return epoch + interval * ceil((now - epoch) / interval)


async def test_auto_pipeline_interval_is_anchored_to_a_fixed_epoch() -> None:
    """The registered interval trigger must fire on an epoch-anchored grid.

    An unanchored IntervalTrigger takes its start_date from construction time,
    so every restart pushes the next run a full interval away and a box that
    reboots daily never reaches a long interval at all.
    """
    from paper_ingestion.scheduler import _INTERVAL_EPOCH  # noqa: PLC0415

    scheduler = await _start(_make_scheduler_app(), 5)
    try:
        trigger = scheduler.get_job("auto_pipeline").trigger
        now = datetime(2026, 6, 1, 9, 30, tzinfo=UTC)
        anchored = IntervalTrigger(hours=5, start_date=_INTERVAL_EPOCH)
        unanchored = IntervalTrigger(hours=5)

        actual = trigger.get_next_fire_time(None, now)
        assert actual == anchored.get_next_fire_time(None, now), (
            "the registered trigger must fire on the fixed-epoch grid"
        )
        assert actual != unanchored.get_next_fire_time(None, now), (
            "an unanchored trigger must not produce the same next fire time"
        )
    finally:
        scheduler.shutdown(wait=False)


async def test_every_registered_job_carries_a_misfire_grace() -> None:
    """Every job start_scheduler registers must tolerate an hour of event-loop stall.

    Iterating the scheduler rather than a list of ids means a job added later —
    including the ones registered by the jobs/ helper modules — cannot silently
    keep APScheduler's one-second default.
    """
    scheduler = await _start(_make_scheduler_app(), 5)
    try:
        jobs = scheduler.get_jobs()
        assert {"data_purge", "purge_magic_link_tokens", "purge_sessions"} <= {
            j.id for j in jobs
        }, "the jobs/ helper modules must have registered, or this check is vacuous"
        offenders = [(j.id, j.misfire_grace_time) for j in jobs if j.misfire_grace_time != 3600]
        assert not offenders, f"jobs without a one-hour misfire grace: {offenders}"
    finally:
        scheduler.shutdown(wait=False)


async def test_reconcile_zotero_poll_job_registers_with_a_misfire_grace() -> None:
    """The live reconcile route registers the same job id as boot, so it needs
    the same misfire semantics; its job is not in get_jobs() after boot."""
    from paper_ingestion.scheduler import reconcile_zotero_poll_job  # noqa: PLC0415

    fake_scheduler = MagicMock()
    with patch(
        "paper_ingestion.scheduler._list_zotero_polling_schedules",
        new=AsyncMock(return_value=[(7, "0 5 * * *")]),
    ):
        await reconcile_zotero_poll_job(
            scheduler=fake_scheduler,
            app=SimpleNamespace(),
            db_pool=MagicMock(),
            user_id=7,
        )

    fake_scheduler.add_job.assert_called_once()
    assert fake_scheduler.add_job.call_args.kwargs["misfire_grace_time"] == 3600


async def test_stale_last_run_schedules_a_catch_up() -> None:
    """A last-run stamp older than the interval means a fire was missed while the
    process was down; jobs live in memory only, so the next fire is pulled in."""
    stale = (datetime.now(UTC) - timedelta(hours=9)).isoformat()
    scheduler = await _start(_make_scheduler_app(last_run=stale), 5)
    try:
        next_run = scheduler.get_job("auto_pipeline").next_run_time
        assert next_run is not None
        assert next_run - datetime.now(UTC) < timedelta(minutes=5)
    finally:
        scheduler.shutdown(wait=False)


async def test_fresh_last_run_schedules_no_catch_up() -> None:
    """A recent successful run means nothing was missed — no extra run at boot."""
    from paper_ingestion.scheduler import _INTERVAL_EPOCH  # noqa: PLC0415

    interval_hours = 5
    fresh = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    booted_at = datetime.now(UTC)
    scheduler = await _start(_make_scheduler_app(last_run=fresh), interval_hours)
    try:
        next_run = scheduler.get_job("auto_pipeline").next_run_time
        assert next_run == _next_grid_point(_INTERVAL_EPOCH, interval_hours, booted_at), (
            "a fresh last-run stamp must leave the job on its anchored grid slot"
        )
    finally:
        scheduler.shutdown(wait=False)


# ---------------------------------------------------------------------------
# Job history stays bounded: a daily prune plus one at boot
# ---------------------------------------------------------------------------


def _app_with_procrastinate() -> tuple[SimpleNamespace, MagicMock, AsyncMock]:
    """Fake app exposing a procrastinate app whose builtin prune task is recorded."""
    from paper_ingestion.scheduler import _REMOVE_OLD_JOBS_TASK  # noqa: PLC0415

    prune_task = MagicMock(defer_async=AsyncMock())
    procrastinate_app = MagicMock(tasks={_REMOVE_OLD_JOBS_TASK: prune_task})
    db_pool = MagicMock(execute=AsyncMock(return_value="DELETE 4"))
    app = SimpleNamespace(
        state=SimpleNamespace(db_pool=db_pool, procrastinate_app=procrastinate_app)
    )
    return app, prune_task, db_pool.execute


async def test_the_builtin_prune_task_is_registered_under_the_expected_key() -> None:
    """A procrastinate upgrade that renames the builtin task must fail loudly here.

    Asserted against the process-wide app the service actually defers through:
    ``add_tasks_from`` re-namespaces the shared builtin blueprint in place, so a
    freshly constructed app carries a differently prefixed key.
    """
    from jarvis_common.task_registry import app as procrastinate_app  # noqa: PLC0415

    from paper_ingestion.scheduler import _REMOVE_OLD_JOBS_TASK  # noqa: PLC0415

    assert _REMOVE_OLD_JOBS_TASK in procrastinate_app.tasks


async def test_a_periodic_job_history_prune_is_registered() -> None:
    """Pruning once per boot never fires again on a long-running deployment."""
    scheduler = await _start(_make_scheduler_app(), 5)
    try:
        job = scheduler.get_job("purge_job_history")
        assert job is not None, "job history must be pruned on a schedule, not only at boot"
        assert job.misfire_grace_time == 3600
    finally:
        scheduler.shutdown(wait=False)


async def test_prune_defers_the_builtin_task_once_and_purges_orphan_progress() -> None:
    """One deferral per run, with the orphaned progress rows swept beside it."""
    from paper_ingestion.scheduler import purge_job_history_task  # noqa: PLC0415

    app, prune_task, execute = _app_with_procrastinate()

    await purge_job_history_task(app)

    prune_task.defer_async.assert_awaited_once()
    kwargs = prune_task.defer_async.await_args.kwargs
    assert kwargs["max_hours"] == 24 * 30
    assert kwargs["remove_failed"] and kwargs["remove_cancelled"] and kwargs["remove_aborted"]
    # Which rows the sweep spares is proven against a real schema in
    # tests/contract/test_job_history_purge_contract.py. Here it is enough that
    # the sweep runs beside the deferral, and runs the service's own statement.
    from paper_ingestion.scheduler import ORPHANED_JOB_PROGRESS_PURGE  # noqa: PLC0415

    execute.assert_awaited_once_with(ORPHANED_JOB_PROGRESS_PURGE)


async def test_boot_prunes_job_history_exactly_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fresh install prunes immediately instead of waiting a day."""
    import importlib  # noqa: PLC0415

    monkeypatch.delitem(sys.modules, "paper_ingestion.main", raising=False)
    main = importlib.import_module("paper_ingestion.main")
    app, prune_task, _execute = _app_with_procrastinate()

    await main._prune_job_history_hook(app)

    prune_task.defer_async.assert_awaited_once()


async def test_catch_up_never_adds_a_second_pipeline_job() -> None:
    """Two ids running one pipeline each carry their own max_instances, so a boot
    whose anchored fire lands in the catch-up window runs the pipeline twice."""
    from paper_ingestion.scheduler import run_auto_pipeline  # noqa: PLC0415

    stale = (datetime.now(UTC) - timedelta(hours=9)).isoformat()
    scheduler = await _start(_make_scheduler_app(last_run=stale), 5)
    try:
        pipeline_jobs = [job for job in scheduler.get_jobs() if job.func is run_auto_pipeline]
        assert [job.id for job in pipeline_jobs] == ["auto_pipeline"]
    finally:
        scheduler.shutdown(wait=False)
