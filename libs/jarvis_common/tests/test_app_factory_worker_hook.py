"""Unit tests for the shared ``make_procrastinate_worker_hook``.

The factory returns the lifespan start hook shared by ``paper_ingestion`` and
``learning_engine``; the per-service variation (register_fn + queue list) is
injected.  Covers the documented post-conditions of the start hook:

- ``register_fn`` is called with the procrastinate ``App`` before the worker starts
- the connector is rebound to the lifespan DSN and opened (job_manager aligned)
- ``set_dependencies`` receives the lifespan-owned pool + http client
- the worker task is spawned with the given queues, signal handlers off,
  named ``procrastinate_worker``, and recorded on ``app.state``

Shutdown is covered separately — ``shutdown_procrastinate_worker`` was already
shared before this factory existed (see paper_ingestion's lifespan tests).
"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from jarvis_common import app_factory
from jarvis_common.app_factory import make_procrastinate_worker_hook

_QUEUES = ["svc_queue", "builtin"]


def test_factory_is_exported() -> None:
    """The factory is part of app_factory's public surface."""
    from jarvis_common import app_factory

    assert "make_procrastinate_worker_hook" in app_factory.__all__


async def test_hook_registers_binds_and_starts_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The returned hook drives the full documented start sequence."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://test/test")

    worker_started = asyncio.Event()
    captured_kwargs: dict[str, Any] = {}

    async def fake_run_worker_async(**kwargs: Any) -> None:
        captured_kwargs.update(kwargs)
        worker_started.set()
        await asyncio.Event().wait()

    fake_proc_app = MagicMock()
    fake_proc_app.run_worker_async = fake_run_worker_async
    fake_proc_app.open_async = AsyncMock(return_value=None)
    fake_proc_app.job_manager = MagicMock()
    # Awaiting a plain MagicMock raises TypeError, which the reclamation's
    # blanket except swallows — the hook would stay green while exercising none
    # of the reclamation path.
    fake_proc_app.job_manager.get_stalled_jobs = AsyncMock(return_value=[])

    register_fn = MagicMock()
    set_dependencies_mock = MagicMock()

    app = FastAPI()
    app.state.db_pool = MagicMock()
    app.state.http_client = MagicMock()

    hook = make_procrastinate_worker_hook(register_fn, queues=["svc_queue", "builtin"])

    with (
        patch("jarvis_common.task_registry.app", fake_proc_app),
        patch("jarvis_common.task_registry.set_dependencies", set_dependencies_mock),
    ):
        await hook(app)
        try:
            await asyncio.wait_for(worker_started.wait(), timeout=2.0)

            # Service-owned registration received the procrastinate App.
            register_fn.assert_called_once_with(fake_proc_app)

            # Connector rebound to the lifespan DSN and opened; job_manager
            # points at the same connector instance.
            from procrastinate.contrib.aiopg import AiopgConnector

            assert isinstance(fake_proc_app.connector, AiopgConnector)
            assert fake_proc_app.job_manager.connector is fake_proc_app.connector
            fake_proc_app.open_async.assert_awaited_once()

            # (pool, http_client) threaded into task_registry, in that order.
            set_dependencies_mock.assert_called_once_with(app.state.db_pool, app.state.http_client)

            # Worker task: injected queues, signal handlers off, named + recorded.
            assert list(captured_kwargs["queues"]) == ["svc_queue", "builtin"]
            assert captured_kwargs["install_signal_handlers"] is False
            assert app.state.procrastinate_app is fake_proc_app
            worker_task = app.state.procrastinate_worker_task
            assert isinstance(worker_task, asyncio.Task)
            assert worker_task.get_name() == "procrastinate_worker"
            assert not worker_task.done()
        finally:
            for task in (app.state.procrastinate_worker_task, app.state.reclaim_task):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task


# ---------------------------------------------------------------------------
# Stalled-job reclamation: startup order and sweep lifecycle
# ---------------------------------------------------------------------------


class _IdlingProcrastinateApp:
    """Records worker starts; run_worker_async returns a coroutine that idles."""

    def __init__(self) -> None:
        self.run_worker_calls: list[list[str]] = []
        self.job_manager = MagicMock()
        self.job_manager.get_stalled_jobs = AsyncMock(return_value=[])
        self.close_async = AsyncMock(return_value=None)

    async def _idle(self) -> None:
        await asyncio.Event().wait()  # runs until the task is cancelled

    def run_worker_async(self, *, queues: list[str], install_signal_handlers: bool):
        self.run_worker_calls.append(queues)
        return self._idle()


async def _cancel(task: asyncio.Task) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_hook_reclaims_before_starting_the_worker_then_sweeps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Startup reclaims abandoned jobs BEFORE the worker starts, then sweeps."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://test/test")
    order: list[str] = []

    async def recording_reclaim(app: FastAPI) -> int:
        order.append("reclaim")
        return 0

    def recording_start(app: FastAPI, queues: list[str]) -> None:
        order.append("start_worker")

    fake_proc_app = MagicMock()
    fake_proc_app.open_async = AsyncMock(return_value=None)
    fake_proc_app.job_manager = MagicMock()

    app = FastAPI()
    app.state.db_pool = MagicMock()
    app.state.http_client = MagicMock()

    hook = make_procrastinate_worker_hook(MagicMock(), queues=_QUEUES)

    with (
        patch("jarvis_common.task_registry.app", fake_proc_app),
        patch("jarvis_common.task_registry.set_dependencies", MagicMock()),
        patch.object(app_factory, "_reclaim_stalled_jobs", recording_reclaim),
        patch.object(app_factory, "_start_worker_task", recording_start),
    ):
        await hook(app)

    assert order == ["reclaim", "start_worker"]

    sweep = app.state.reclaim_task
    assert isinstance(sweep, asyncio.Task)
    assert sweep.get_name() == "reclaim_stalled_jobs"
    assert not sweep.done()
    await _cancel(sweep)


async def test_sweep_is_paused_resumed_and_cancelled_with_the_worker(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sweep writes to the job table, so it follows the worker's lifecycle."""
    soft = tmp_path / ".maintenance"
    monkeypatch.setenv("MAINTENANCE_SENTINEL", str(soft))
    monkeypatch.setenv("MAINTENANCE_DESTRUCTIVE_SENTINEL", str(tmp_path / ".destructive"))
    monkeypatch.setattr(app_factory, "check_migrations", AsyncMock())

    proc = _IdlingProcrastinateApp()
    app = SimpleNamespace(
        state=SimpleNamespace(
            procrastinate_app=proc,
            procrastinate_worker_task=None,
            db_pool=MagicMock(),
            reclaim_task=None,
        )
    )
    app_factory._start_worker_task(app, _QUEUES)
    app.state.reclaim_task = asyncio.create_task(
        app_factory._reclaim_stalled_jobs_forever(app), name="reclaim_stalled_jobs"
    )

    paused = app.state.reclaim_task

    # A restore raises the sentinel: the sweep stops writing alongside the worker.
    # Clearing the handle alone would leave an uncancelled task issuing job-table
    # writes for the whole restore, so the task itself must be cancelled.
    soft.touch()
    assert await app_factory._maintenance_watcher_step(app, _QUEUES, was_active=False) is True
    assert paused.cancelled()
    assert app.state.reclaim_task is None

    # The restore clears: the sweep is running again, or nothing ever reclaims.
    soft.unlink()
    assert await app_factory._maintenance_watcher_step(app, _QUEUES, was_active=True) is False
    resumed = app.state.reclaim_task
    assert isinstance(resumed, asyncio.Task)
    assert resumed.get_name() == "reclaim_stalled_jobs"
    assert not resumed.done()

    # Shutdown stops it before the connector closes, so it cannot fire on a
    # closed pool during lifespan teardown.
    await app_factory.shutdown_procrastinate_worker(app)
    assert resumed.cancelled()
    assert app.state.reclaim_task is None


async def test_restore_resume_keeps_writers_stopped_when_schema_check_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale restore cannot restart a worker after the read-only check fails."""

    async def stale_schema(_pool: Any) -> None:
        raise RuntimeError("database schema is at version 113")

    monkeypatch.setattr(app_factory, "check_migrations", stale_schema)
    proc = _IdlingProcrastinateApp()
    app = SimpleNamespace(
        state=SimpleNamespace(
            db_pool=MagicMock(),
            procrastinate_app=proc,
            procrastinate_worker_task=None,
        )
    )

    with pytest.raises(RuntimeError, match="database schema"):
        await app_factory._resume_after_maintenance(app, _QUEUES)

    assert proc.run_worker_calls == []
    assert app.state.procrastinate_worker_task is None
