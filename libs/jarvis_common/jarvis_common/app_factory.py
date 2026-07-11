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
from starlette.types import ASGIApp, Receive, Scope, Send
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from jarvis_common.auth import (
    RAW_CLIENT_SCOPE_KEY,
    invalidate_api_key_login_cache,
    refresh_allowed_networks_cache,
    refresh_api_key_cache,
    validate_production_config,
)
from jarvis_common.config import get_jarvis_common_settings
from jarvis_common.correlation_middleware import CorrelationIdMiddleware
from jarvis_common.crypto import reload_fernet_on_sighup, validate_encrypted_config_rows
from jarvis_common.db_helpers import init_pg_connection, invalidate_effective_num_ctx_cache
from jarvis_common.error_handlers import (
    generic_exception_handler,
    http_exception_handler,
    validation_exception_handler,
)
from jarvis_common.http_rate_limiter import rate_limit_exceeded_handler
from jarvis_common.maintenance import configure_maintenance, maintenance_active
from jarvis_common.migrations import run_migrations
from jarvis_common.request_id import RequestIDMiddleware
from jarvis_common.settings import get_core_settings, get_secrets_settings

logger = logging.getLogger(__name__)

_POSTGRES_SECRET_PATH = "/run/secrets/postgres_password"

# instructor.Mode member that emits grammar-constrained decoding
STRUCTURED_DECODING_MODE = "JSON_SCHEMA"


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


def _start_worker_task(app: FastAPI, queues: list[str]) -> None:
    """Spawn the procrastinate worker loop as a background task on ``app.state``.

    Shared by the initial worker hook and the maintenance watcher's resume path
    so the ``create_task`` invariant (name, ``install_signal_handlers=False``)
    lives in one place. ``app.state.procrastinate_app`` must already be open.
    """
    procrastinate_app = app.state.procrastinate_app
    app.state.procrastinate_worker_task = asyncio.create_task(
        procrastinate_app.run_worker_async(
            queues=queues,
            install_signal_handlers=False,
        ),
        name="procrastinate_worker",
    )


async def _pause_worker_task(app: FastAPI) -> None:
    """Cancel the worker loop but KEEP the connector open (distinct from shutdown).

    A restore must stop the loop's ``UPDATE procrastinate_jobs`` fetch/lock writes
    without closing the connector, so :func:`_start_worker_task` can restart the
    loop on the same open ``procrastinate_app`` once the restore clears.
    """
    task = getattr(app.state, "procrastinate_worker_task", None)
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    app.state.procrastinate_worker_task = None


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


class RawClientStashMiddleware:
    """Snapshot the real transport peer before ProxyHeaders rewrites it (M5).

    Pure-ASGI and minimal on purpose: no Request construction, one scope write.
    MUST be registered AFTER ProxyHeadersMiddleware (Starlette: last-added =
    outermost = runs first) so it sees the ORIGINAL ``scope["client"]`` — the
    actual socket peer — and stashes it under
    :data:`jarvis_common.auth.RAW_CLIENT_SCOPE_KEY` before
    ProxyHeadersMiddleware overwrites ``scope["client"]`` in place from
    X-Forwarded-For. The X-Owner-User-Id guard in :mod:`jarvis_common.auth`
    requires BOTH this raw peer AND the rewritten client to be allowlisted, so
    a forged XFF from a non-allowlisted source can no longer spoof its way
    onto the owner-override path.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            # Key PRESENCE (even with a None value) tells the auth guard the
            # middleware ran; the guard fails safe (rejects) when the stash
            # exists but carries no usable IP.
            scope[RAW_CLIENT_SCOPE_KEY] = scope.get("client")
        await self.app(scope, receive, send)


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
    4. ProxyHeadersMiddleware (decodes XFF -- rewrites scope["client"] in place)
    5. RawClientStashMiddleware (added last -- outermost, snapshots the raw
       socket peer BEFORE ProxyHeadersMiddleware rewrites scope["client"])

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
    assert cors_origins is not None
    # Fail-fast BEFORE installing the wildcard CORS middleware so a
    # misconfigured production deploy crashes at startup, not at request time.
    if cors_origins == ["*"] and get_core_settings().environment.lower() == "production":
        raise RuntimeError(
            "CORS wildcard (allow_origins=['*']) is not allowed in ENVIRONMENT=production. "
            "Set DEV_CORS_OPEN=false and configure CORS_ORIGINS explicitly."
        )
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

    # 4. ProxyHeadersMiddleware -- rewrites scope["client"] from X-Forwarded-For.
    # uvicorn vs starlette ASGI-scope stubs are nominally incompatible despite
    # being structurally identical; safe to ignore until upstream stubs reconcile.
    app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=trusted_proxy_hosts)  # type: ignore[reportArgumentType]

    # 5. RawClientStashMiddleware -- added AFTER ProxyHeadersMiddleware so it runs
    # OUTSIDE it (Starlette: later-added = more outer = runs first), snapshotting
    # the raw socket peer BEFORE ProxyHeadersMiddleware mutates scope["client"]
    # from X-Forwarded-For. The X-Owner-User-Id guard (jarvis_common.auth) requires
    # BOTH the stashed raw peer AND the rewritten client to be allowlisted (M5).
    app.add_middleware(RawClientStashMiddleware)

    # 6. MaintenanceMiddleware -- added last in the shared stack so it runs ahead
    # of the rest of it, short-circuiting non-exempt requests with 503 while a
    # fresh restore sentinel exists. Pure-ASGI: the non-maintenance path passes
    # through untouched, so SSE/streaming routes are unaffected. A per-service
    # SessionMiddleware added afterward (paper_ingestion) wraps further outside,
    # so an authenticated request still resolves its session before the 503 --
    # the restore's DB safety comes from restore.sh revoking DB connections, not
    # from this user-facing gate.
    configure_maintenance(app)

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

    Shared lifespan hook used by both ``paper_ingestion`` and
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
        mode=instructor.Mode[STRUCTURED_DECODING_MODE],
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


def make_procrastinate_worker_hook(
    register_fn: Callable[[Any], None],
    queues: list[str],
) -> LifespanHook:
    """Return a lifespan hook that starts the procrastinate worker for a service.

    ``paper_ingestion`` and ``learning_engine`` previously carried near-identical
    ``_start_procrastinate_worker`` hooks differing only in the task-register
    function and the queue list — those two variations are injected here.  The
    teardown counterpart is already shared (:func:`shutdown_procrastinate_worker`).

    The returned hook wires the procrastinate ``App`` connector to the same DSN
    backing ``app.state.db_pool`` (reads from ``/run/secrets/postgres_password``;
    falls back to ``DATABASE_URL`` in tests), calls ``register_fn`` BEFORE the
    worker starts, opens the connector, threads ``(pool, http_client)`` into
    ``task_registry`` so task wrappers can access the shared singletons, then
    starts the worker as a background asyncio task.  Stored on ``app.state``
    for the symmetric teardown.

    Parameters
    ----------
    register_fn:
        Service-owned callable receiving the procrastinate ``App`` and
        registering the service's kind→handler mapping (dependency inversion —
        the service keeps its register import deferred inside this callable,
        so jarvis_common never imports service code).
    queues:
        Queue names the worker polls (e.g. ``["paper_ingestion", "builtin"]``).
    """

    async def _hook(app: FastAPI) -> None:
        from procrastinate.contrib.aiopg import AiopgConnector  # noqa: PLC0415

        from jarvis_common.task_registry import (  # noqa: PLC0415
            app as procrastinate_app,
        )
        from jarvis_common.task_registry import (
            set_dependencies,
        )

        # Register kind→handler mappings BEFORE the worker starts.
        register_fn(procrastinate_app)

        # The connector built at task_registry import time has no DSN — replace it
        # with one bound to the same DSN the app_factory pool uses (reads from
        # /run/secrets/postgres_password; falls back to DATABASE_URL in tests).
        procrastinate_app.connector = AiopgConnector(dsn=build_database_url())
        procrastinate_app.job_manager.connector = procrastinate_app.connector
        await procrastinate_app.open_async()

        set_dependencies(app.state.db_pool, app.state.http_client)

        app.state.procrastinate_app = procrastinate_app
        _start_worker_task(app, queues)

    return _hook


async def _resume_after_maintenance(app: FastAPI, queues: list[str]) -> None:
    """Reconcile a restored (possibly older) schema, then resume background writers.

    Ordered so writers never observe a stale schema or stale DB-derived cache:
    forward-migrate first (lock-42-serialized, idempotent), drop the in-process
    caches whose backing ``user_config`` rows a restore may have rolled back, then
    restart the worker loop. Task 4.6 will extend this branch with a
    secrets-rotated self-exit; keeping it a discrete function preserves that seam.
    """
    await run_migrations(app.state.db_pool)
    invalidate_api_key_login_cache()
    invalidate_effective_num_ctx_cache()
    _start_worker_task(app, queues)
    logger.info("maintenance: reconciled schema and resumed writers")


async def _maintenance_watcher_step(app: FastAPI, queues: list[str], *, was_active: bool) -> bool:
    """Run one watcher poll; return the current maintenance-active state.

    ``inactive→active`` cancels the worker loop (queue hygiene during a restore);
    ``active→inactive`` reconciles the schema and resumes writers.
    """
    now_active = maintenance_active()
    if now_active and not was_active:
        await _pause_worker_task(app)
        logger.info("maintenance: paused background writers")
    elif was_active and not now_active:
        await _resume_after_maintenance(app, queues)
    return now_active


async def _maintenance_watcher_loop(
    app: FastAPI, queues: list[str], poll_interval_s: float
) -> None:
    """Poll the maintenance sentinel, pausing/resuming the worker loop across it.

    Pauses immediately when maintenance is already active at start (covers a
    service restart that lands mid-restore, e.g. 4.6's self-restart). Never dies:
    a failed tick is logged and retried on the next poll.
    """
    was_active = maintenance_active()
    if was_active:
        await _pause_worker_task(app)
        logger.info("maintenance: paused background writers")
    while True:
        try:
            await asyncio.sleep(poll_interval_s)
            was_active = await _maintenance_watcher_step(app, queues, was_active=was_active)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("maintenance watcher tick failed", exc_info=True)


def make_maintenance_watcher_hook(
    queues: list[str], *, poll_interval_s: float = 5.0
) -> LifespanHook:
    """Return an init hook that starts the maintenance watcher background task.

    Wire it into ``custom_init_tasks`` immediately AFTER the procrastinate worker
    hook, with :func:`shutdown_maintenance_watcher` at the index-aligned teardown
    slot so LIFO cleanup stops the watcher BEFORE the worker's connector closes.
    """

    async def _hook(app: FastAPI) -> None:
        app.state.maintenance_watcher_task = asyncio.create_task(
            _maintenance_watcher_loop(app, queues, poll_interval_s),
            name="maintenance_watcher",
        )

    return _hook


async def shutdown_maintenance_watcher(app: FastAPI) -> None:
    """Cancel the maintenance watcher task (teardown counterpart of its hook)."""
    task = getattr(app.state, "maintenance_watcher_task", None)
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    app.state.maintenance_watcher_task = None


__all__ = [
    "ServiceLifespanConfig",
    "configure_lifespan",
    "configure_middleware_and_errors",
    "init_langfuse_hook",
    "make_init_langfuse_hook",
    "make_maintenance_watcher_hook",
    "make_procrastinate_worker_hook",
    "shutdown_maintenance_watcher",
]
