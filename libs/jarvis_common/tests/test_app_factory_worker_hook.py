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
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from jarvis_common.app_factory import make_procrastinate_worker_hook


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
            app.state.procrastinate_worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await app.state.procrastinate_worker_task
