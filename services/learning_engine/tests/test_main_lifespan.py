"""Lifespan tests for learning_engine — B.4 Step 2 procrastinate worker wiring.

Mirrors ``services/paper_ingestion/tests/test_main_lifespan.py`` for the
learning_engine service: the new ``_start_procrastinate_worker`` /
``_shutdown_procrastinate_worker`` hooks must spawn a named asyncio task
on the ``learning_engine`` + ``builtin`` queues, thread the lifespan-owned
pool + http_client into ``task_registry`` via ``set_dependencies``, and
clean up cleanly on shutdown. Legacy ``worker_loop`` wiring is preserved
during cutover (asserted structurally on the real ``_lifespan_config``).
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture()
def fake_pool() -> AsyncMock:
    pool = AsyncMock()
    pool.close = AsyncMock()
    return pool


@pytest.fixture()
def fake_http_client() -> AsyncMock:
    client = AsyncMock()
    client.aclose = AsyncMock()
    return client


def _patch_factory_io(fake_pool: AsyncMock, fake_http_client: AsyncMock) -> list[Any]:
    return [
        patch(
            "jarvis_common.app_factory.asyncpg.create_pool",
            AsyncMock(return_value=fake_pool),
        ),
        patch(
            "jarvis_common.app_factory.validate_encrypted_config_rows",
            AsyncMock(return_value=None),
        ),
        patch(
            "jarvis_common.app_factory.validate_production_config",
            MagicMock(return_value=None),
        ),
        patch(
            "jarvis_common.app_factory.httpx.AsyncClient",
            MagicMock(return_value=fake_http_client),
        ),
    ]


# ---------------------------------------------------------------------------
# Structural assertions on the real _lifespan_config
# ---------------------------------------------------------------------------


def test_lifespan_config_includes_procrastinate_hooks() -> None:
    """The real main.py config must wire both the start + shutdown hook.

    Guards the dual-write contract: legacy ``worker_loop`` (driven by the
    factory's own ``start_jobs_worker`` when ``jobs_worker_kinds`` non-empty)
    and the new procrastinate worker MUST coexist during cutover.
    """
    from learning_engine.main import (
        _lifespan_config,
        _shutdown_procrastinate_worker,
        _start_procrastinate_worker,
    )

    assert _start_procrastinate_worker in _lifespan_config.custom_init_tasks
    init_idx = _lifespan_config.custom_init_tasks.index(_start_procrastinate_worker)
    assert _lifespan_config.custom_teardown_tasks[init_idx] is _shutdown_procrastinate_worker

    # Legacy worker still wired — card.generate is one of the legacy kinds.
    assert "card.generate" in _lifespan_config.jobs_worker_kinds


# ---------------------------------------------------------------------------
# Behavioural test of the broker hook itself, via a minimal lifespan
# ---------------------------------------------------------------------------


class TestProcrastinateWorkerLifespan:
    async def test_start_and_shutdown_hooks_drive_worker_lifecycle(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_pool: AsyncMock,
        fake_http_client: AsyncMock,
    ) -> None:
        monkeypatch.setenv("DATABASE_URL", "postgresql://test/test")

        worker_started = asyncio.Event()
        worker_cancelled = asyncio.Event()
        captured_kwargs: dict[str, Any] = {}

        async def fake_run_worker_async(**kwargs: Any) -> None:
            captured_kwargs.update(kwargs)
            worker_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                worker_cancelled.set()
                raise

        fake_proc_app = MagicMock()
        fake_proc_app.run_worker_async = fake_run_worker_async
        fake_proc_app.open_async = AsyncMock(return_value=None)
        fake_proc_app.close_async = AsyncMock(return_value=None)
        fake_proc_app.connector = MagicMock()
        fake_proc_app.job_manager = MagicMock()

        set_dependencies_mock = MagicMock()

        from jarvis_common.app_factory import ServiceLifespanConfig, configure_lifespan
        from learning_engine.main import (
            _shutdown_procrastinate_worker,
            _start_procrastinate_worker,
        )

        minimal_config = ServiceLifespanConfig(
            service_name="test_learning_engine_lifespan",
            jobs_worker_kinds=set(),
            custom_init_tasks=[_start_procrastinate_worker],
            custom_teardown_tasks=[_shutdown_procrastinate_worker],
        )

        with contextlib.ExitStack() as stack:
            for p in _patch_factory_io(fake_pool, fake_http_client):
                stack.enter_context(p)
            stack.enter_context(patch("jarvis_common.task_registry.app", fake_proc_app))
            stack.enter_context(
                patch(
                    "jarvis_common.task_registry.set_dependencies",
                    set_dependencies_mock,
                )
            )

            from fastapi import FastAPI

            app = FastAPI()
            lifespan = configure_lifespan(minimal_config)
            async with lifespan(app):
                await asyncio.wait_for(worker_started.wait(), timeout=2.0)

                assert list(captured_kwargs.get("queues", [])) == [
                    "learning_engine",
                    "builtin",
                ]
                assert captured_kwargs.get("install_signal_handlers") is False

                set_dependencies_mock.assert_called_once_with(
                    app.state.db_pool, app.state.http_client
                )
                assert app.state.db_pool is fake_pool
                assert app.state.http_client is fake_http_client

                worker_task = app.state.procrastinate_worker_task
                assert isinstance(worker_task, asyncio.Task)
                assert worker_task.get_name() == "procrastinate_worker"
                assert not worker_task.done()

                fake_proc_app.open_async.assert_awaited_once()

            assert worker_cancelled.is_set()
            assert app.state.procrastinate_worker_task.done()
            fake_proc_app.close_async.assert_awaited()
