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
The factory deliberately does not own scheduler creation, source-singleton
initialization, FSRS construction, or any other domain object -- those
remain in the service so the factory does not need to know about them.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
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

from jarvis_common.auth import (
    refresh_allowed_networks_cache,
    refresh_api_key_cache,
    validate_production_config,
)
from jarvis_common.config import get_jarvis_common_settings
from jarvis_common.correlation_middleware import CorrelationIdMiddleware
from jarvis_common.crypto import reload_fernet_on_sighup, validate_encrypted_config_rows
from jarvis_common.db_helpers import init_pg_connection
from jarvis_common.error_handlers import (
    generic_exception_handler,
    http_exception_handler,
    validation_exception_handler,
)
from jarvis_common.http_rate_limiter import rate_limit_exceeded_handler
from jarvis_common.request_id import RequestIDMiddleware
from jarvis_common.settings import get_core_settings, get_secrets_settings

logger = logging.getLogger(__name__)

_POSTGRES_SECRET_PATH = "/run/secrets/postgres_password"


def build_database_url() -> str:
    """Construct the PostgreSQL DSN without embedding a password in any env var.

    Resolution order:
    1. ``/run/secrets/postgres_password`` (Docker Secret mount — preferred; avoids
       leaking the password via ``/proc/<pid>/environ`` or ``docker inspect``).
    2. ``DATABASE_URL`` environment variable (legacy / test fallback).

    The DSN is built as ``postgresql://<user>:<pass>@postgres:5432/<db>``
    using ``POSTGRES_USER`` and ``POSTGRES_DB`` env vars (both default to
    ``jarvis`` matching ``docker-compose.yml``).

    Raises
    ------
    RuntimeError
        If neither the secret file nor ``DATABASE_URL`` can be read.

    """
    from pathlib import Path  # noqa: PLC0415

    secret_file = Path(_POSTGRES_SECRET_PATH)
    if secret_file.is_file():
        password = secret_file.read_text().strip()
        if not password:
            raise RuntimeError(
                f"FATAL: {_POSTGRES_SECRET_PATH} exists but is empty — "
                "cannot construct DATABASE_URL"
            )
        settings = get_jarvis_common_settings()
        user = settings.postgres_user
        db = settings.postgres_db
        return f"postgresql://{user}:{password}@postgres:5432/{db}"

    # Fallback: tests and local dev set DATABASE_URL directly.
    url = get_jarvis_common_settings().database_url
    if url:
        return url

    raise RuntimeError(
        f"Cannot build DATABASE_URL: {_POSTGRES_SECRET_PATH} is absent and "
        "DATABASE_URL is not set. "
        "In Docker, ensure the postgres_password secret is mounted. "
        "In tests, set the DATABASE_URL env var."
    )


_HTTP_CLIENT_DEFAULTS: dict[str, Any] = {
    "timeout": httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0),
}


_DB_POOL_DEFAULTS: dict[str, Any] = {
    "min_size": 2,
    "max_size": 10,
}


LifespanHook = Callable[[FastAPI], Awaitable[None]]


async def shutdown_procrastinate_worker(app: FastAPI) -> None:
    """Cancel the procrastinate worker task and close the connector."""
    task = getattr(app.state, "procrastinate_worker_task", None)
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    procrastinate_app = getattr(app.state, "procrastinate_app", None)
    if procrastinate_app is not None:
        with contextlib.suppress(Exception):
            await procrastinate_app.close_async()


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
    custom_init_tasks:
        Async callables run AFTER the DB pool, http client, and (optionally)
        encrypted-config validation have completed, but BEFORE the jobs worker
        starts.  Each receives ``app`` as its single argument and must mutate
        ``app.state`` to install service-specific singletons.  Tasks run
        sequentially in declared order.
    custom_teardown_tasks:
        Async callables run BEFORE the http client and DB pool are closed
        (the jobs worker is already stopped by the time these run).  Useful
        for shutting down APScheduler, Qdrant clients, etc.  Run in reverse
        (LIFO) order relative to their corresponding init hooks, matching the
        standard resource-stack cleanup convention.

    """

    service_name: str
    db_pool_settings: dict[str, Any] = field(default_factory=dict)
    http_client_kwargs: dict[str, Any] = field(default_factory=dict)
    custom_init_tasks: list[LifespanHook] = field(default_factory=list)
    custom_teardown_tasks: list[LifespanHook | None] = field(default_factory=list)
    # custom_init_tasks and custom_teardown_tasks MUST have equal length. For
    # init hooks that have no teardown counterpart, pad with None at the same
    # index. The runtime asserts the contract on startup and raises ValueError
    # on mismatch.


def _resolve_db_pool_kwargs(overrides: dict[str, Any]) -> dict[str, Any]:
    """Merge defaults + env vars + per-service overrides for ``asyncpg.create_pool``."""
    merged: dict[str, Any] = {**_DB_POOL_DEFAULTS, **overrides}
    settings = get_jarvis_common_settings()
    merged["min_size"] = (
        int(settings.db_pool_min) if settings.db_pool_min is not None else int(merged["min_size"])
    )
    merged["max_size"] = (
        int(settings.db_pool_max) if settings.db_pool_max is not None else int(merged["max_size"])
    )
    merged.setdefault("init", init_pg_connection)
    return merged


def _log_auth_status() -> None:
    """Log the API-key/DEV_MODE configuration once at startup.

    Also refreshes the module-level caches so that any key/CIDR rotation that
    happened between import time and startup (e.g. Docker secret mount settling)
    takes effect without a service restart.
    """
    refresh_api_key_cache()
    try:
        refresh_allowed_networks_cache()
    except Exception:  # noqa: BLE001
        logger.warning(
            "refresh_allowed_networks_cache failed at startup (invalid CIDR?)", exc_info=True
        )
    dev_mode = get_core_settings().dev_mode
    secret = get_secrets_settings().jarvis_api_key
    api_key = secret.get_secret_value() if secret else ""
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
    3. ``httpx.AsyncClient`` creation with the service-specific timeout
    4. ``validate_encrypted_config_rows`` so encrypted secrets fail-fast on boot
       (before any custom hook runs; schema-missing errors are downgraded to a warning)
    5. each ``custom_init_tasks`` hook in order; each hook's teardown
       counterpart is registered on an ``AsyncExitStack`` immediately after the
       hook succeeds — if a hook raises, only teardowns for completed inits run
       (LIFO), eliminating the previous double-execution bug
    6. auth-config status log line
    7. yield
    8. each registered teardown in LIFO order (last-init = first-torn-down)
    9. http client close, then DB pool close

    Custom hooks may raise to abort startup.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        validate_production_config()
        # Register SIGHUP handler so operators can rotate CONFIG_ENC_KEY without
        # a full process restart.  No-op on platforms without SIGHUP.
        reload_fernet_on_sighup()

        # AsyncExitStack guarantees LIFO cleanup of every resource pushed onto
        # it, regardless of where startup fails.  Only resources that were
        # successfully acquired are cleaned up — there is no double-execution
        # risk and no need for a manual compensating-teardown path.
        async with AsyncExitStack() as stack:
            database_url = build_database_url()
            pool_kwargs = _resolve_db_pool_kwargs(config.db_pool_settings)
            db_pool = await asyncpg.create_pool(database_url, **pool_kwargs)
            stack.push_async_callback(db_pool.close)
            app.state.db_pool = db_pool

            http_kwargs = {**_HTTP_CLIENT_DEFAULTS, **config.http_client_kwargs}
            http_client = httpx.AsyncClient(**http_kwargs)
            stack.push_async_callback(http_client.aclose)
            app.state.http_client = http_client

            # Validate encrypted config rows BEFORE custom hooks so a bad
            # schema fails fast and hooks never see a partially-initialized DB.
            try:
                await validate_encrypted_config_rows(db_pool)
            except (asyncpg.UndefinedTableError, asyncpg.UndefinedColumnError):
                logger.warning(
                    "validate_encrypted_config_rows skipped: user_config table not yet available"
                    " (fresh DB before migrations run)"
                )

            # Equal-length contract: each init hook MUST have a corresponding
            # teardown hook at the same index (use None as a placeholder for
            # hooks without a teardown).  zip() would silently truncate on
            # mismatch; assert eagerly to surface the bug at startup.
            if len(config.custom_init_tasks) != len(config.custom_teardown_tasks):
                raise ValueError(
                    f"custom_init_tasks ({len(config.custom_init_tasks)}) and "
                    f"custom_teardown_tasks ({len(config.custom_teardown_tasks)}) "
                    f"must have equal length; pad with None for hooks without teardown."
                )

            # Run each custom init hook; immediately register its teardown
            # counterpart on the stack so cleanup is paired at each step.
            # If any init hook raises, only already-registered teardowns run
            # (LIFO via AsyncExitStack) — no double-execution, no skipped cleanup.
            for init_hook, teardown_hook in zip(
                config.custom_init_tasks, config.custom_teardown_tasks, strict=True
            ):
                await init_hook(app)
                if teardown_hook is not None:
                    stack.push_async_callback(teardown_hook, app)

            _log_auth_status()

            logger.info("%s started", config.service_name)
            yield

        logger.info("%s stopped", config.service_name)

    return lifespan


def configure_middleware_and_errors(
    app: FastAPI,
    *,
    limiter: Any,
    cors_origins: list[str] | None = None,
    trusted_proxy_hosts: str | list[str] = "*",
) -> None:
    """Register the shared middleware stack + standardized error handlers.

    Middleware order (Starlette: last-added = outermost = runs first):

    1. RequestIDMiddleware (innermost -- emits X-Request-Id)
    1b. CorrelationIdMiddleware (wraps RequestID -- reads/emits X-Correlation-Id)
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
    # 1. RequestIDMiddleware (innermost — emits X-Request-Id first).
    app.add_middleware(RequestIDMiddleware)
    # 1b. CorrelationIdMiddleware — wraps RequestID so the correlation layer can
    #     read the already-assigned request-ID header from the outer scope.
    app.add_middleware(CorrelationIdMiddleware)

    # 2. SlowAPIMiddleware -- pre-auth global cap.
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)  # type: ignore[arg-type]
    app.add_middleware(SlowAPIMiddleware)

    # 3. CORSMiddleware
    if cors_origins is None:
        if get_core_settings().dev_cors_open:
            cors_origins = ["*"]
        else:
            cors_origins = get_jarvis_common_settings().cors_origins_list
    # allow_credentials=True is incompatible with allow_origins=["*"]
    # (Starlette raises a warning and the browser rejects the response).
    # When the caller explicitly passes ["*"] — or the env var is set to "*" —
    # we omit the flag so the server at least returns a usable (non-credentialed)
    # CORS response rather than an invalid one.
    use_credentials = "*" not in cors_origins
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization", "X-API-Key"],
        allow_credentials=use_credentials,
    )

    # 4. ProxyHeadersMiddleware -- outermost.
    # uvicorn vs starlette ASGI-scope stubs are nominally incompatible despite
    # being structurally identical; safe to ignore until upstream stubs reconcile.
    app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=trusted_proxy_hosts)  # type: ignore[reportArgumentType]

    # Standardized error handlers
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, validation_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, generic_exception_handler)


async def init_langfuse_hook(
    app: FastAPI,
    *,
    set_services_callback: Callable[[Any], None] | None = None,
) -> None:
    """Initialize Langfuse SDK and Instructor-patched OpenAI client.

    DOM-J-01: shared lifespan hook used by both ``paper_ingestion`` and
    ``learning_engine``.  No-op when ``LANGFUSE_HOST`` is unset (local dev
    without ``--profile observability``).  Attaches ``app.state.openai_client``
    for use by ``call_llm_structured`` in request handlers and job workers.

    Parameters
    ----------
    app:
        The FastAPI application.  ``app.state.openai_client`` is set on it.
    set_services_callback:
        Optional callable receiving the Instructor-patched OpenAI client to
        populate a per-service module-level holder (e.g. ``paper_ingestion._state.set_services``
        or ``learning_engine._state.set_services``).  When provided, called as
        ``set_services_callback(openai_client)``.

    """
    import instructor  # noqa: PLC0415
    import openai  # noqa: PLC0415

    from jarvis_common.llm_client import (  # noqa: PLC0415
        _langfuse_lifespan_hook,
        get_litellm_config,
    )

    _langfuse_lifespan_hook()
    litellm_config = get_litellm_config()
    _master_key_secret = get_secrets_settings().litellm_master_key
    openai_client = instructor.from_openai(
        openai.AsyncOpenAI(
            base_url=f"{litellm_config.base_url}/v1",
            api_key=_master_key_secret.get_secret_value() if _master_key_secret else "dummy",
        ),
        mode=instructor.Mode.JSON,
    )
    app.state.openai_client = openai_client
    if set_services_callback is not None:
        set_services_callback(openai_client)


def make_init_langfuse_hook(
    set_services_callback: Callable[[Any], None] | None = None,
) -> LifespanHook:
    """Return an ``init_langfuse_hook`` bound to a per-service ``set_services`` callback.

    Convenience factory for use in :class:`ServiceLifespanConfig.custom_init_tasks`,
    where each hook must have signature ``(app: FastAPI) -> Awaitable[None]``.
    """

    async def _hook(app: FastAPI) -> None:
        await init_langfuse_hook(app, set_services_callback=set_services_callback)

    return _hook


__all__ = [
    "ServiceLifespanConfig",
    "configure_lifespan",
    "configure_middleware_and_errors",
    "init_langfuse_hook",
    "make_init_langfuse_hook",
]
