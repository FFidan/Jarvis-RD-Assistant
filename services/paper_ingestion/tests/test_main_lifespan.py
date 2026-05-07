"""Lifespan tests for paper_ingestion — B.4 Step 4 procrastinate worker wiring.

Covers the ``_start_procrastinate_worker`` /
``_shutdown_procrastinate_worker`` hooks added in Task B.2:

- the procrastinate worker is created as a named asyncio.Task during startup
- ``set_dependencies`` is called with the lifespan-owned pool + http_client
- on shutdown the worker task is cancelled cleanly without an unawaited
  coroutine warning, and the procrastinate connector is closed.

We don't drive the full real lifespan (real run_migrations, telegram
bootstrap, scheduler init, etc. all need live DBs/HTTP) — instead we
construct a minimal ``ServiceLifespanConfig`` that exercises ONLY the
broker hook and assert the documented post-conditions. The full main.py
config is checked by structural assertions on the hook list.
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
    """The real main.py config must wire both the start + shutdown hook."""
    from paper_ingestion.main import (
        _lifespan_config,
        _shutdown_procrastinate_worker,
        _start_procrastinate_worker,
    )

    assert _start_procrastinate_worker in _lifespan_config.custom_init_tasks
    init_idx = _lifespan_config.custom_init_tasks.index(_start_procrastinate_worker)
    # Same index in teardown list = compensating teardown wiring.
    assert _lifespan_config.custom_teardown_tasks[init_idx] is _shutdown_procrastinate_worker


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
        """Start hook spawns named worker on the right queues; shutdown hook cancels cleanly."""
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
        from paper_ingestion.main import (
            _shutdown_procrastinate_worker,
            _start_procrastinate_worker,
        )

        # Minimal config: just the two procrastinate hooks and no other init noise.
        minimal_config = ServiceLifespanConfig(
            service_name="test_paper_ingestion_lifespan",
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

                # Worker was started with the paper_ingestion-specific queues +
                # builtin (so procrastinate's auto-registered remove_old_jobs
                # cleanup task runs out of this service).
                assert list(captured_kwargs.get("queues", [])) == [
                    "paper_ingestion",
                    "builtin",
                ]
                # Signal handlers stay off — we manage cancellation from the
                # lifespan, not from SIGINT/SIGTERM in the worker.
                assert captured_kwargs.get("install_signal_handlers") is False

                # set_dependencies received the lifespan-owned pool + http_client
                # (the ones bound to app.state by configure_lifespan).
                set_dependencies_mock.assert_called_once_with(
                    app.state.db_pool, app.state.http_client
                )
                assert app.state.db_pool is fake_pool
                assert app.state.http_client is fake_http_client

                # Worker task is recorded on app.state with the expected name.
                worker_task = app.state.procrastinate_worker_task
                assert isinstance(worker_task, asyncio.Task)
                assert worker_task.get_name() == "procrastinate_worker"
                assert not worker_task.done()

                # Connector was opened.
                fake_proc_app.open_async.assert_awaited_once()

            # On exit the procrastinate worker was cancelled cleanly and the
            # connector was closed.
            assert worker_cancelled.is_set()
            assert app.state.procrastinate_worker_task.done()
            fake_proc_app.close_async.assert_awaited()


# ---------------------------------------------------------------------------
# _autoconfigure_models_hook structural + unit tests
# ---------------------------------------------------------------------------


def test_lifespan_config_includes_autoconfigure_hook() -> None:
    """_autoconfigure_models_hook must be registered and have a None teardown counterpart."""
    from paper_ingestion.main import _autoconfigure_models_hook, _lifespan_config

    assert _autoconfigure_models_hook in _lifespan_config.custom_init_tasks
    idx = _lifespan_config.custom_init_tasks.index(_autoconfigure_models_hook)
    assert _lifespan_config.custom_teardown_tasks[idx] is None


@pytest.mark.asyncio
async def test_autoconfigure_models_hook_sets_flag_and_writes_user_config() -> None:
    """On first boot, the hook detects tier and writes llm.* + autoconfigured flag."""
    from unittest.mock import AsyncMock, MagicMock

    from fastapi import FastAPI
    from paper_ingestion.main import _autoconfigure_models_hook
    from paper_ingestion.services.model_lifecycle import HardwareInfo

    pool = MagicMock()
    conn = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire.return_value = ctx

    # No user_config rows exist yet (first boot) — all fetchrow calls return None.
    conn.fetchrow.return_value = None

    tier1_hw = HardwareInfo(
        vram_gb=8.0, vram_source="nvidia-smi", tier=1, detected_at="2026-05-06T00:00:00+00:00"
    )

    app = FastAPI()
    app.state.db_pool = pool

    with (
        patch(
            "paper_ingestion.services.model_lifecycle.detect_hardware",
            return_value=tier1_hw,
        ),
        patch(
            "paper_ingestion.services.model_lifecycle.recommendations_for_role",
            return_value=[{"id": "qwen3:4b", "status": "downloadable", "tier": 1}],
        ),
        patch(
            "paper_ingestion.services.litellm_config.update_litellm_model",
            new=AsyncMock(return_value=True),
        ),
    ):
        await _autoconfigure_models_hook(app)

    # At least 3 INSERT calls: one per role (smart, fast, embed) + flag.
    execute_calls = [str(call) for call in conn.execute.await_args_list]
    insert_calls = [c for c in execute_calls if "INSERT INTO user_config" in c]
    assert len(insert_calls) >= 4  # 3 roles + autoconfigured flag


@pytest.mark.asyncio
async def test_autoconfigure_models_hook_is_idempotent() -> None:
    """When flag is already set, the hook returns early without writing anything."""
    from unittest.mock import AsyncMock, MagicMock

    from fastapi import FastAPI
    from paper_ingestion.main import _autoconfigure_models_hook

    pool = MagicMock()
    conn = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire.return_value = ctx

    # _rehydrate returns None (no stored prefs), but autoconfigured flag IS present.
    conn.fetchrow.side_effect = [
        None,  # llm.smart_model in _rehydrate
        None,  # llm.fast_model in _rehydrate
        None,  # llm.embed_model in _rehydrate
        {"value": "true"},  # system.models_autoconfigured → already set
    ]

    app = FastAPI()
    app.state.db_pool = pool

    with patch(
        "paper_ingestion.services.litellm_config.update_litellm_model",
        new=AsyncMock(return_value=True),
    ):
        await _autoconfigure_models_hook(app)

    # No INSERT should have been executed (idempotent early-return).
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_rehydrate_litellm_aliases_skips_no_db_connected(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When LiteLLM returns HTTP 400 No DB Connected, rehydrate logs and skips."""
    import logging
    from unittest.mock import AsyncMock, MagicMock

    from paper_ingestion.main import _rehydrate_litellm_aliases

    pool = MagicMock()
    conn = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire.return_value = ctx

    # Return a row for every key so update_litellm_model is actually called.
    conn.fetchrow.return_value = {"value": "qwen3:4b"}

    no_db_exc = RuntimeError(
        "LiteLLM /config/update failed for alias 'smart': HTTP 400 No DB Connected"
    )

    with (
        caplog.at_level(logging.INFO, logger="paper_ingestion.main"),
        patch(
            "paper_ingestion.services.litellm_config.update_litellm_model",
            new=AsyncMock(side_effect=no_db_exc),
        ),
    ):
        # Must NOT raise.
        await _rehydrate_litellm_aliases(pool)

    assert any(
        "no admin db attached" in record.message.lower()
        for record in caplog.records
        if record.levelno == logging.INFO
    ), f"Expected INFO log about 'no admin db attached'; got: {[r.message for r in caplog.records]}"


@pytest.mark.asyncio
async def test_rehydrate_litellm_aliases_reraises_502() -> None:
    """Non-400 / non-'No DB Connected' RuntimeErrors propagate unchanged."""
    from unittest.mock import AsyncMock, MagicMock

    from paper_ingestion.main import _rehydrate_litellm_aliases

    pool = MagicMock()
    conn = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire.return_value = ctx

    conn.fetchrow.return_value = {"value": "qwen3:4b"}

    gateway_exc = RuntimeError(
        "LiteLLM /config/update failed for alias 'smart': HTTP 502 Bad Gateway"
    )

    with (
        patch(
            "paper_ingestion.services.litellm_config.update_litellm_model",
            new=AsyncMock(side_effect=gateway_exc),
        ),
        pytest.raises(RuntimeError, match="HTTP 502"),
    ):
        await _rehydrate_litellm_aliases(pool)
