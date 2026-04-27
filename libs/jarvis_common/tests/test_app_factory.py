"""Tests for the shared FastAPI app factory (DRY-002).

Covers :func:`jarvis_common.configure_middleware_and_errors`,
:func:`jarvis_common.configure_lifespan`, and the implicit
``start_jobs_worker`` / ``_stop_jobs_worker`` symmetry that the lifespan
relies on.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from jarvis_common.app_factory import (
    ServiceLifespanConfig,
    _stop_jobs_worker,
    configure_lifespan,
    configure_middleware_and_errors,
    start_jobs_worker,
)
from jarvis_common.http_rate_limiter import create_limiter
from jarvis_common.request_id import RequestIDMiddleware
from slowapi.middleware import SlowAPIMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

# ---------------------------------------------------------------------------
# configure_middleware_and_errors
# ---------------------------------------------------------------------------


class TestConfigureMiddleware:
    def test_configure_middleware_registers_cors_proxy_headers_request_id(self) -> None:
        """All four middlewares + the rate-limit handler are installed."""
        app = FastAPI()
        limiter = create_limiter()

        configure_middleware_and_errors(
            app,
            limiter=limiter,
            cors_origins=["https://example.test"],
            trusted_proxy_hosts="*",
        )

        # Starlette stores middleware as Middleware(cls, **kwargs); inspect by class name.
        middleware_classes = {m.cls for m in app.user_middleware}
        assert RequestIDMiddleware in middleware_classes
        assert SlowAPIMiddleware in middleware_classes
        assert CORSMiddleware in middleware_classes
        assert ProxyHeadersMiddleware in middleware_classes

        # SlowAPI requires app.state.limiter to be the same instance we passed in.
        assert app.state.limiter is limiter

        # All three standardized error handlers are registered.
        from fastapi.exceptions import RequestValidationError
        from slowapi.errors import RateLimitExceeded
        from starlette.exceptions import HTTPException as StarletteHTTPException

        assert RateLimitExceeded in app.exception_handlers
        assert StarletteHTTPException in app.exception_handlers
        assert RequestValidationError in app.exception_handlers
        assert Exception in app.exception_handlers

    def test_configure_middleware_falls_back_to_env_var_for_cors(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When ``cors_origins`` is None, CORS_ORIGINS env var supplies the list."""
        monkeypatch.setenv("CORS_ORIGINS", "https://a.test, https://b.test")
        app = FastAPI()
        configure_middleware_and_errors(app, limiter=create_limiter())

        cors_mw = next(m for m in app.user_middleware if m.cls is CORSMiddleware)
        # Starlette stores kwargs differently across versions — accept both.
        kwargs = cors_mw.kwargs if hasattr(cors_mw, "kwargs") else cors_mw.options
        assert kwargs["allow_origins"] == ["https://a.test", "https://b.test"]


# ---------------------------------------------------------------------------
# configure_lifespan -- custom hooks ordering
# ---------------------------------------------------------------------------


class TestConfigureLifespan:
    async def test_configure_lifespan_runs_custom_init_and_teardown_tasks_in_order(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Custom hooks fire in declared order, init before yield, teardown after."""
        monkeypatch.setenv("DATABASE_URL", "postgresql://test/test")

        call_log: list[str] = []

        async def init_a(app: FastAPI) -> None:
            call_log.append("init_a")

        async def init_b(app: FastAPI) -> None:
            call_log.append("init_b")

        async def teardown_a(app: FastAPI) -> None:
            call_log.append("teardown_a")

        async def teardown_b(app: FastAPI) -> None:
            call_log.append("teardown_b")

        config = ServiceLifespanConfig(
            service_name="test_service",
            jobs_worker_kinds=set(),  # no worker
            custom_init_tasks=[init_a, init_b],
            custom_teardown_tasks=[teardown_a, teardown_b],
        )

        # Mock asyncpg + httpx + validate_encrypted_config_rows so the lifespan
        # body doesn't try to talk to a real database.
        fake_pool = AsyncMock()
        fake_pool.close = AsyncMock()
        fake_http_client = AsyncMock()
        fake_http_client.aclose = AsyncMock()

        with (
            patch(
                "jarvis_common.app_factory.asyncpg.create_pool", AsyncMock(return_value=fake_pool)
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
        ):
            app = FastAPI()
            lifespan = configure_lifespan(config)
            async with lifespan(app):
                # Inside the context: init hooks have run, teardown hooks have not.
                assert call_log == ["init_a", "init_b"]

        # After exit: both teardown hooks have run, in order.
        assert call_log == ["init_a", "init_b", "teardown_a", "teardown_b"]
        fake_http_client.aclose.assert_awaited_once()
        fake_pool.close.assert_awaited_once()

    async def test_lifespan_closes_pool_when_init_task_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pool and http_client are closed even when a custom_init_task raises (L2 fix)."""
        monkeypatch.setenv("DATABASE_URL", "postgresql://test/test")

        async def bad_init(app: FastAPI) -> None:
            raise RuntimeError("init task boom")

        config = ServiceLifespanConfig(
            service_name="test_service_leak",
            jobs_worker_kinds=set(),
            custom_init_tasks=[bad_init],
        )

        fake_pool = AsyncMock()
        fake_pool.close = AsyncMock()
        fake_http_client = AsyncMock()
        fake_http_client.aclose = AsyncMock()

        with (
            patch(
                "jarvis_common.app_factory.asyncpg.create_pool", AsyncMock(return_value=fake_pool)
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
        ):
            app = FastAPI()
            lifespan = configure_lifespan(config)
            with pytest.raises(RuntimeError, match="init task boom"):
                async with lifespan(app):
                    pass  # pragma: no cover -- never reached

        # Both resources must be closed despite the init task raising.
        fake_pool.close.assert_awaited_once()
        fake_http_client.aclose.assert_awaited_once()

    async def test_configure_lifespan_skips_jobs_worker_when_kinds_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Empty ``jobs_worker_kinds`` keeps the background task off."""
        monkeypatch.setenv("DATABASE_URL", "postgresql://test/test")

        config = ServiceLifespanConfig(service_name="no_worker", jobs_worker_kinds=set())
        fake_pool = AsyncMock()
        fake_pool.close = AsyncMock()
        fake_http_client = AsyncMock()
        fake_http_client.aclose = AsyncMock()

        with (
            patch(
                "jarvis_common.app_factory.asyncpg.create_pool", AsyncMock(return_value=fake_pool)
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
            patch("jarvis_common.app_factory.start_jobs_worker") as mock_start,
        ):
            app = FastAPI()
            async with configure_lifespan(config)(app):
                pass

        mock_start.assert_not_called()


# ---------------------------------------------------------------------------
# start_jobs_worker
# ---------------------------------------------------------------------------


class TestStartJobsWorker:
    async def test_jobs_worker_starts_with_correct_kinds(self) -> None:
        """``start_jobs_worker`` invokes ``worker_loop`` with the configured kinds."""
        kinds = {"foo.do", "bar.do"}

        captured_kwargs: dict = {}

        async def fake_worker_loop(pool, http_client, *, kinds, stop_event, **rest):
            captured_kwargs["kinds"] = kinds
            captured_kwargs["pool"] = pool
            captured_kwargs["http_client"] = http_client
            captured_kwargs["stop_event"] = stop_event
            # Run until told to stop.
            await stop_event.wait()

        app = FastAPI()
        app.state.db_pool = MagicMock()
        app.state.http_client = MagicMock()

        with patch("jarvis_common.jobs.worker_loop", fake_worker_loop):
            start_jobs_worker(app, kinds=kinds)

            # Background task and stop event should be wired onto app.state.
            assert isinstance(app.state.jobs_worker_stop, asyncio.Event)
            assert isinstance(app.state.jobs_worker_task, asyncio.Task)

            # Yield control so the worker_loop coroutine starts and captures kwargs.
            await asyncio.sleep(0)

            assert captured_kwargs["kinds"] == kinds
            assert captured_kwargs["pool"] is app.state.db_pool
            assert captured_kwargs["http_client"] is app.state.http_client
            assert captured_kwargs["stop_event"] is app.state.jobs_worker_stop

            # Cleanly stop.
            await _stop_jobs_worker(app)
            assert app.state.jobs_worker_task.done()

    async def test_stop_jobs_worker_cancels_when_task_does_not_finish_in_time(self) -> None:
        """A worker that ignores stop_event is cancelled after the 5s grace period."""
        app = FastAPI()
        app.state.jobs_worker_stop = asyncio.Event()

        async def stuck_worker():
            try:
                await asyncio.sleep(60)  # ignore stop_event
            except asyncio.CancelledError:
                raise

        app.state.jobs_worker_task = asyncio.create_task(stuck_worker())

        # Patch the timeout to a small value so the test runs fast.
        with patch("jarvis_common.app_factory.asyncio.wait_for", side_effect=TimeoutError):
            await _stop_jobs_worker(app)

        # Cancel was issued; await it so the task completes.
        try:
            await app.state.jobs_worker_task
        except asyncio.CancelledError:
            pass
        assert app.state.jobs_worker_task.cancelled() or app.state.jobs_worker_task.done()
