"""Tests for Sprint 5 Wave 1 Batch 1A fixes.

Covers:
- H4: _stop_jobs_worker awaits the task after cancel() to drain CancelledError
- H5: validate_encrypted_config_rows tolerates missing table on fresh DB
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest
from fastapi import FastAPI  # noqa: F401

# ---------------------------------------------------------------------------
# H4 — _stop_jobs_worker awaits task after cancel
# ---------------------------------------------------------------------------


class TestStopJobsWorkerAwaitsAfterCancel:
    async def test_stop_jobs_worker_awaits_after_cancel(self) -> None:
        """After task.cancel(), _stop_jobs_worker must await the task to drain CancelledError."""
        from jarvis_common.app_factory import _stop_jobs_worker

        app = FastAPI()
        app.state.jobs_worker_stop = asyncio.Event()

        # Build a real asyncio.Task wrapping a coroutine that blocks until cancelled.
        async def blocking_worker() -> None:
            await asyncio.sleep(3600)

        task = asyncio.create_task(blocking_worker())
        app.state.jobs_worker_task = task

        # Simulate wait_for timing out so the cancel branch is exercised.
        with patch(
            "jarvis_common.app_factory.asyncio.wait_for",
            side_effect=TimeoutError,
        ):
            await _stop_jobs_worker(app)

        # After _stop_jobs_worker returns, the task must be done (cancelled).
        assert task.done(), "task should be done after _stop_jobs_worker drains CancelledError"
        assert task.cancelled(), "task should be in cancelled state"

    async def test_stop_jobs_worker_suppresses_cancelled_error_on_await(self) -> None:
        """_stop_jobs_worker must not propagate CancelledError raised by await task."""
        from jarvis_common.app_factory import _stop_jobs_worker

        app = FastAPI()
        app.state.jobs_worker_stop = asyncio.Event()

        async def cancellable_worker() -> None:
            await asyncio.sleep(3600)

        task = asyncio.create_task(cancellable_worker())
        app.state.jobs_worker_task = task

        # Patch wait_for to raise CancelledError (as if the lifespan itself was cancelled).
        with patch(
            "jarvis_common.app_factory.asyncio.wait_for",
            side_effect=asyncio.CancelledError,
        ):
            # Must NOT raise — _stop_jobs_worker should suppress via contextlib.suppress.
            await _stop_jobs_worker(app)

        assert task.done()


# ---------------------------------------------------------------------------
# H5 — validate_encrypted_config_rows tolerates missing table
# ---------------------------------------------------------------------------


class TestValidateEncryptedConfigRowsToleratesMissingTable:
    async def test_lifespan_tolerates_undefined_table_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """UndefinedTableError from validate_encrypted_config_rows must not abort startup."""
        from jarvis_common.app_factory import ServiceLifespanConfig, configure_lifespan

        monkeypatch.setenv("DATABASE_URL", "postgresql://test/test")

        fake_pool = AsyncMock()
        fake_pool.close = AsyncMock()
        fake_http_client = AsyncMock()
        fake_http_client.aclose = AsyncMock()

        # Simulate fresh DB: user_config table does not exist yet.
        with (
            patch(
                "jarvis_common.app_factory.asyncpg.create_pool",
                AsyncMock(return_value=fake_pool),
            ),
            patch(
                "jarvis_common.app_factory.validate_encrypted_config_rows",
                AsyncMock(side_effect=asyncpg.UndefinedTableError("relation does not exist")),
            ),
            patch(
                "jarvis_common.app_factory.validate_production_config",
                MagicMock(return_value=None),
            ),
            patch(
                "jarvis_common.app_factory.httpx.AsyncClient",
                MagicMock(return_value=fake_http_client),
            ),
        ):
            config = ServiceLifespanConfig(
                service_name="test_fresh_db",
                jobs_worker_kinds=set(),
            )
            app = FastAPI()
            # Must not raise — the UndefinedTableError should be caught and warned.
            async with configure_lifespan(config)(app):
                pass  # startup succeeded

    async def test_lifespan_tolerates_undefined_column_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """UndefinedColumnError from validate_encrypted_config_rows must not abort startup."""
        from jarvis_common.app_factory import ServiceLifespanConfig, configure_lifespan

        monkeypatch.setenv("DATABASE_URL", "postgresql://test/test")

        fake_pool = AsyncMock()
        fake_pool.close = AsyncMock()
        fake_http_client = AsyncMock()
        fake_http_client.aclose = AsyncMock()

        with (
            patch(
                "jarvis_common.app_factory.asyncpg.create_pool",
                AsyncMock(return_value=fake_pool),
            ),
            patch(
                "jarvis_common.app_factory.validate_encrypted_config_rows",
                AsyncMock(side_effect=asyncpg.UndefinedColumnError("column does not exist")),
            ),
            patch(
                "jarvis_common.app_factory.validate_production_config",
                MagicMock(return_value=None),
            ),
            patch(
                "jarvis_common.app_factory.httpx.AsyncClient",
                MagicMock(return_value=fake_http_client),
            ),
        ):
            config = ServiceLifespanConfig(
                service_name="test_fresh_db_col",
                jobs_worker_kinds=set(),
            )
            app = FastAPI()
            async with configure_lifespan(config)(app):
                pass  # startup succeeded

    async def test_validate_encrypted_config_rows_called_after_custom_init_tasks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """validate_encrypted_config_rows must be called BEFORE custom_init_tasks run.

        NEW-M6: validation order was intentionally reversed so that a bad schema
        fails fast before any custom hook runs.
        """
        from jarvis_common.app_factory import ServiceLifespanConfig, configure_lifespan

        monkeypatch.setenv("DATABASE_URL", "postgresql://test/test")

        call_log: list[str] = []

        async def init_hook(app: FastAPI) -> None:
            call_log.append("init_hook")

        async def fake_validate(pool: object, **kwargs: object) -> int:
            call_log.append("validate_encrypted")
            return 0

        fake_pool = AsyncMock()
        fake_pool.close = AsyncMock()
        fake_http_client = AsyncMock()
        fake_http_client.aclose = AsyncMock()

        with (
            patch(
                "jarvis_common.app_factory.asyncpg.create_pool",
                AsyncMock(return_value=fake_pool),
            ),
            patch(
                "jarvis_common.app_factory.validate_encrypted_config_rows",
                fake_validate,
            ),
            patch(
                "jarvis_common.app_factory.validate_production_config",
                MagicMock(return_value=None),
            ),
            patch(
                "jarvis_common.app_factory.httpx.AsyncClient",
                MagicMock(return_value=fake_http_client),
            ),
        ):
            config = ServiceLifespanConfig(
                service_name="test_ordering",
                jobs_worker_kinds=set(),
                custom_init_tasks=[init_hook],
                custom_teardown_tasks=[None],
            )
            app = FastAPI()
            async with configure_lifespan(config)(app):
                pass

        # validate_encrypted must come before init_hook (NEW-M6 fix).
        assert call_log == ["validate_encrypted", "init_hook"], (
            f"Expected validate_encrypted before init_hook, got: {call_log}"
        )
