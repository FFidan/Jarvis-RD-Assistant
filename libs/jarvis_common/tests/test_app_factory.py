"""Tests for the shared FastAPI app factory (DRY-002).

Covers :func:`jarvis_common.configure_middleware_and_errors` and
:func:`jarvis_common.configure_lifespan`.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from jarvis_common.app_factory import (
    ServiceLifespanConfig,
    configure_lifespan,
    configure_middleware_and_errors,
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

    def test_cors_middleware_allows_credentials_for_concrete_origins(self) -> None:
        """allow_credentials=True when origins is a concrete list (not wildcard)."""
        app = FastAPI()
        configure_middleware_and_errors(
            app,
            limiter=create_limiter(),
            cors_origins=["https://app.example.test"],
        )

        cors_mw = next(m for m in app.user_middleware if m.cls is CORSMiddleware)
        kwargs = cors_mw.kwargs if hasattr(cors_mw, "kwargs") else cors_mw.options
        assert kwargs.get("allow_credentials") is True

    def test_cors_middleware_omits_credentials_for_wildcard_origins(self) -> None:
        """allow_credentials must be False/absent when origins is [\"*\"] to stay spec-compliant."""
        app = FastAPI()
        configure_middleware_and_errors(
            app,
            limiter=create_limiter(),
            cors_origins=["*"],
        )

        cors_mw = next(m for m in app.user_middleware if m.cls is CORSMiddleware)
        kwargs = cors_mw.kwargs if hasattr(cors_mw, "kwargs") else cors_mw.options
        # allow_credentials must not be True when origins is wildcard.
        assert kwargs.get("allow_credentials") is not True


# ---------------------------------------------------------------------------
# configure_lifespan -- custom hooks ordering
# ---------------------------------------------------------------------------


class TestConfigureLifespan:
    async def test_configure_lifespan_runs_custom_init_and_teardown_tasks_in_order(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Custom hooks fire in declared order, init before yield, teardown after in LIFO order."""
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

        # After exit: both teardown hooks have run in LIFO (reverse-init) order.
        # AsyncExitStack runs teardowns LIFO, so teardown_b (pushed last) runs
        # before teardown_a.
        assert call_log == ["init_a", "init_b", "teardown_b", "teardown_a"]
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
            custom_init_tasks=[bad_init],
            custom_teardown_tasks=[None],
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
