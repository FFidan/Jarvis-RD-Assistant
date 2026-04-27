"""Shared FastAPI application factory.

DRY-002: Both ``paper_ingestion`` and ``learning_engine`` had hand-rolled
lifespan managers and middleware/error-handler registration that diverged
only in service-specific init/teardown steps.  This module extracts the
common skeleton so each service collapses to a configuration object plus
a small set of service-specific hooks.

Public surface
--------------
* :class:`ServiceLifespanConfig` -- per-service configuration object.
* :func:`configure_lifespan` -- builds the async context manager that
  drives DB pool creation, http client setup, custom init hooks, jobs
  worker start, and the symmetric teardown sequence.
* :func:`configure_middleware_and_errors` -- registers the
  RequestID/SlowAPI/CORS/ProxyHeaders middleware stack and the standardized
  validation/HTTP/generic error handlers.
* :func:`start_jobs_worker` -- starts ``jarvis_common.jobs.worker_loop`` as
  a background task with a stop-event for graceful shutdown.

The factory deliberately does not own scheduler creation, source-singleton
initialization, FSRS construction, or any other domain object -- those
remain in the service so the factory does not need to know about them.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import asyncpg
import httpx
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from jarvis_common.auth import refresh_api_key_cache, validate_production_config
from jarvis_common.crypto import validate_encrypted_config_rows
from jarvis_common.db_helpers import init_pg_connection
from jarvis_common.error_handlers import (
    generic_exception_handler,
    http_exception_handler,
    validation_exception_handler,
)
from jarvis_common.http_rate_limiter import rate_limit_exceeded_handler
from jarvis_common.request_id import RequestIDMiddleware
from jarvis_common.secrets import read_secret

logger = logging.getLogger(__name__)


_HTTP_CLIENT_DEFAULTS: dict[str, Any] = {
    "timeout": httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0),
}


_DB_POOL_DEFAULTS: dict[str, Any] = {
    "min_size": 2,
    "max_size": 10,
}


LifespanHook = Callable[[FastAPI], Awaitable[None]]


@dataclass
class ServiceLifespanConfig:
    """Per-service configuration consumed by :func:`configure_lifespan`.

    Parameters
    ----------
    service_name:
        Human-readable tag used in startup/shutdown log lines.
    db_pool_settings:
        Overrides for ``asyncpg.create_pool`` keyword arguments.  Keys recognised
        here override the defaults of ``min_size=2``, ``max_size=10`` (the
        ``DB_POOL_MIN`` / ``DB_POOL_MAX`` environment variables still take
        precedence and are read at lifespan start).
    http_client_kwargs:
        Overrides for ``httpx.AsyncClient`` keyword arguments.  Defaults to a
        long-poll timeout (``connect=10s, read=120s, write=30s, pool=10s``).
    jobs_worker_kinds:
        Set of job kinds this service's worker should poll for.  Pass an
        empty set to disable the jobs worker entirely.
    custom_init_tasks:
        Async callables run AFTER the DB pool, http client, and (optionally)
        encrypted-config validation have completed, but BEFORE the jobs worker
        starts.  Each receives ``app`` as its single argument and must mutate
        ``app.state`` to install service-specific singletons.  Tasks run
        sequentially in declared order.
    custom_teardown_tasks:
        Async callables run BEFORE the http client and DB pool are closed
        (the jobs worker is already stopped by the time these run).  Useful
        for shutting down APScheduler, Qdrant clients, etc.  Run in declared
        order.
    """

    service_name: str
    db_pool_settings: dict[str, Any] = field(default_factory=dict)
    http_client_kwargs: dict[str, Any] = field(default_factory=dict)
    jobs_worker_kinds: set[str] = field(default_factory=set)
    custom_init_tasks: list[LifespanHook] = field(default_factory=list)
    custom_teardown_tasks: list[LifespanHook] = field(default_factory=list)


def _resolve_db_pool_kwargs(overrides: dict[str, Any]) -> dict[str, Any]:
    """Merge defaults + env vars + per-service overrides for ``asyncpg.create_pool``."""
    merged: dict[str, Any] = {**_DB_POOL_DEFAULTS, **overrides}
    min_env = os.environ.get("DB_POOL_MIN")
    max_env = os.environ.get("DB_POOL_MAX")
    merged["min_size"] = int(min_env) if min_env is not None else int(merged["min_size"])
    merged["max_size"] = int(max_env) if max_env is not None else int(merged["max_size"])
    merged.setdefault("init", init_pg_connection)
    return merged


def _log_auth_status() -> None:
    """Log the API-key/DEV_MODE configuration once at startup.

    Also refreshes the module-level API-key cache so that any key rotation that
    happened between import time and startup (e.g. Docker secret mount settling)
    takes effect without a service restart.
    """
    refresh_api_key_cache()
    dev_mode = os.environ.get("DEV_MODE", "false").lower() == "true"
    api_key = read_secret("JARVIS_API_KEY")
    if api_key:
        logger.info("API key authentication enabled")
    elif dev_mode:
        logger.info("DEV_MODE enabled -- running without authentication")
    else:
        logger.warning(
            "JARVIS_API_KEY not set and DEV_MODE not enabled -- service will reject requests"
        )


def configure_lifespan(config: ServiceLifespanConfig) -> Callable[[FastAPI], Any]:
    """Return a FastAPI ``lifespan`` async context manager.

    The returned callable is suitable to pass directly to ``FastAPI(lifespan=...)``.
    It performs:

    1. ``validate_production_config()``
    2. asyncpg pool creation (env-var-tunable size, with ``init_pg_connection``)
    3. ``validate_encrypted_config_rows`` so encrypted secrets fail-fast on boot
    4. ``httpx.AsyncClient`` creation with the service-specific timeout
    5. each ``custom_init_tasks`` hook in order
    6. auth-config status log line
    7. jobs worker start (if ``jobs_worker_kinds`` non-empty)
    8. yield
    9. jobs worker stop (graceful 5s, then cancel)
    10. each ``custom_teardown_tasks`` hook in order
    11. http client + db pool close

    Custom hooks may raise to abort startup.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        validate_production_config()

        db_pool = None
        http_client = None
        try:
            database_url = os.environ["DATABASE_URL"]
            pool_kwargs = _resolve_db_pool_kwargs(config.db_pool_settings)
            db_pool = await asyncpg.create_pool(database_url, **pool_kwargs)
            app.state.db_pool = db_pool

            http_kwargs = {**_HTTP_CLIENT_DEFAULTS, **config.http_client_kwargs}
            http_client = httpx.AsyncClient(**http_kwargs)
            app.state.http_client = http_client

            for hook in config.custom_init_tasks:
                await hook(app)

            try:
                await validate_encrypted_config_rows(db_pool)
            except (asyncpg.UndefinedTableError, asyncpg.UndefinedColumnError):
                logger.warning(
                    "validate_encrypted_config_rows skipped: user_config table not yet available"
                    " (fresh DB before migrations run)"
                )

            _log_auth_status()

            if config.jobs_worker_kinds:
                start_jobs_worker(app, kinds=config.jobs_worker_kinds)

            logger.info("%s started", config.service_name)
            yield
        finally:
            await _stop_jobs_worker(app)

            for hook in config.custom_teardown_tasks:
                try:
                    await hook(app)
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "Custom teardown hook failed during %s shutdown", config.service_name
                    )

            if http_client is not None:
                try:
                    await http_client.aclose()
                except Exception:  # noqa: BLE001
                    logger.warning("http_client.aclose() failed", exc_info=True)
            if db_pool is not None:
                try:
                    await db_pool.close()
                except Exception:  # noqa: BLE001
                    logger.warning("db_pool.close() failed", exc_info=True)
            logger.info("%s stopped", config.service_name)

    return lifespan


def start_jobs_worker(app: FastAPI, *, kinds: set[str]) -> None:
    """Start the shared ``jarvis_common.jobs.worker_loop`` background task.

    Stores ``app.state.jobs_worker_stop`` (asyncio.Event) and
    ``app.state.jobs_worker_task`` (asyncio.Task) for the symmetric stop in
    :func:`_stop_jobs_worker`.
    """
    from jarvis_common import jobs as jobs_lib

    stop_event = asyncio.Event()
    app.state.jobs_worker_stop = stop_event
    app.state.jobs_worker_task = asyncio.create_task(
        jobs_lib.worker_loop(
            app.state.db_pool,
            app.state.http_client,
            kinds=kinds,
            stop_event=stop_event,
        )
    )


async def _stop_jobs_worker(app: FastAPI) -> None:
    """Signal the jobs worker to stop and wait up to 5s before cancelling."""
    stop_event = getattr(app.state, "jobs_worker_stop", None)
    task = getattr(app.state, "jobs_worker_task", None)
    if stop_event is None or task is None:
        return
    stop_event.set()
    try:
        await asyncio.wait_for(task, timeout=5.0)
    except (TimeoutError, asyncio.CancelledError):
        logger.warning("jobs worker did not stop in time -- cancelling task")
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


def configure_middleware_and_errors(
    app: FastAPI,
    *,
    limiter: Any,
    cors_origins: list[str] | None = None,
    trusted_proxy_hosts: str | list[str] = "*",
) -> None:
    """Register the shared middleware stack + standardized error handlers.

    Middleware order (Starlette: last-added = outermost = runs first):

    1. RequestIDMiddleware (added first -- innermost)
    2. SlowAPIMiddleware (rate limiting)
    3. CORSMiddleware
    4. ProxyHeadersMiddleware (added last -- outermost, decodes XFF first)

    Parameters
    ----------
    limiter:
        SlowAPI ``Limiter`` instance.  Stored on ``app.state.limiter`` (which
        ``SlowAPIMiddleware`` reads on each request).
    cors_origins:
        Whitelist of allowed CORS origins.  When ``None``, falls back to the
        ``CORS_ORIGINS`` environment variable (comma-separated, default
        ``https://localhost:3001``).
    trusted_proxy_hosts:
        Hosts that ``ProxyHeadersMiddleware`` will trust ``X-Forwarded-*``
        headers from.  Pass ``"*"`` to trust any (matches the
        learning_engine default); pass a comma-separated string or a list to
        restrict.
    """
    # 1. RequestIDMiddleware
    app.add_middleware(RequestIDMiddleware)

    # 2. SlowAPIMiddleware -- pre-auth global cap.
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)  # type: ignore[arg-type]
    app.add_middleware(SlowAPIMiddleware)

    # 3. CORSMiddleware
    if cors_origins is None:
        cors_origins = [
            o.strip()
            for o in os.environ.get("CORS_ORIGINS", "https://localhost:3001").split(",")
            if o.strip()
        ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization", "X-API-Key"],
    )

    # 4. ProxyHeadersMiddleware -- outermost.
    app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=trusted_proxy_hosts)

    # Standardized error handlers
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, validation_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, generic_exception_handler)


__all__ = [
    "ServiceLifespanConfig",
    "configure_lifespan",
    "configure_middleware_and_errors",
    "start_jobs_worker",
]
