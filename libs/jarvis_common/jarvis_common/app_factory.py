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
import os
import signal
import time
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import asyncpg
import httpx
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIASGIMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Receive, Scope, Send
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from jarvis_common.auth import (
    RAW_CLIENT_SCOPE_KEY,
    invalidate_api_key_login_cache,
    refresh_api_key_cache,
    validate_production_config,
)
from jarvis_common.config import POSTGRES_PASSWORD_SECRET_PATH, get_jarvis_common_settings
from jarvis_common.correlation_middleware import CorrelationIdMiddleware
from jarvis_common.crypto import reload_fernet_on_sighup, validate_encrypted_config_rows
from jarvis_common.db_helpers import init_pg_connection, invalidate_effective_num_ctx_cache
from jarvis_common.error_handlers import (
    generic_exception_handler,
    http_exception_handler,
    validation_exception_handler,
)
from jarvis_common.http_rate_limiter import rate_limit_exceeded_handler
from jarvis_common.job_context import ProcrastinateJobContextShim
from jarvis_common.maintenance import (
    configure_maintenance,
    maintenance_active,
    secrets_rotated_since,
)
from jarvis_common.migrations import check_migrations
from jarvis_common.pinned_transport import JARVIS_SERVICE_POLICY, PinnedAsyncTransport
from jarvis_common.request_id import RequestIDMiddleware
from jarvis_common.settings import get_core_settings, get_secrets_settings
from jarvis_common.telemetry import configure_telemetry, flush_telemetry

logger = logging.getLogger(__name__)

# instructor.Mode member that emits grammar-constrained decoding
STRUCTURED_DECODING_MODE = "JSON_SCHEMA"


def build_database_url(
    *,
    user: str | None = None,
    password_file: str | os.PathLike[str] | None = None,
) -> str:
    """Construct the PostgreSQL DSN without embedding a password in any env var.

    Runtime callers must provide both ``user`` and ``password_file``. This
    prevents an API process from silently reconnecting with the legacy database
    owner after role-specific credentials are deployed. ``DATABASE_URL`` remains
    the established direct fallback for local development and tests.

    The DSN is built as ``postgresql://<user>:<pass>@postgres:5432/<db>``
    using the configured user, password file, host, port, and database.
    ``<user>``, ``<pass>`` and ``<db>`` are percent-encoded so values containing DSN-reserved
    characters (``@ : / ? # %``) do not corrupt the URL.

    Raises
    ------
    RuntimeError
        If the explicit runtime credential is incomplete, unreadable, or empty
        and no direct test/local ``DATABASE_URL`` is configured.

    """
    from pathlib import Path  # noqa: PLC0415

    settings = get_jarvis_common_settings()
    if user is not None or password_file is not None:
        if password_file is None and settings.database_url:
            return settings.database_url
        if not user or password_file is None:
            raise RuntimeError("PostgreSQL runtime credentials require user and password file")
        secret_file = Path(password_file)
        if not secret_file.is_file():
            raise RuntimeError(f"PostgreSQL password file is unavailable: {secret_file}")
        password = secret_file.read_text().strip()
        if not password:
            raise RuntimeError("PostgreSQL password file is empty")
        encoded_user = quote(user, safe="")
        password = quote(password, safe="")
        db = quote(settings.postgres_db, safe="")
        return (
            f"postgresql://{encoded_user}:{password}@{settings.postgres_host}:"
            f"{settings.postgres_port}/{db}"
        )

    # Compatibility for direct utility/test callers. Application lifespans
    # always pass their configured role and password file above.
    secret_file = Path(POSTGRES_PASSWORD_SECRET_PATH)
    if secret_file.is_file():
        password = secret_file.read_text().strip()
        if not password:
            raise RuntimeError(f"FATAL: {POSTGRES_PASSWORD_SECRET_PATH} exists but is empty")
        legacy_user = quote(settings.postgres_user or "jarvis", safe="")
        encoded_password = quote(password, safe="")
        db = quote(settings.postgres_db, safe="")
        return f"postgresql://{legacy_user}:{encoded_password}@postgres:5432/{db}"

    # Fallback: tests and local dev set DATABASE_URL directly.
    url = settings.database_url
    if url:
        return url

    raise RuntimeError(
        "Cannot build DATABASE_URL: runtime credentials are not configured and "
        "DATABASE_URL is not set"
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


_STALLED_HEARTBEAT_SECONDS = 120  # tolerates 12 missed 10s heartbeats (procrastinate default)

_RECLAIM_INTERVAL_SECONDS = _STALLED_HEARTBEAT_SECONDS + 30  # strictly greater than the
# threshold so a dead worker's heartbeat has aged out by the first sweep after it

_INTERRUPTED_JOB_ERROR = {
    "message": (
        "The service was interrupted while this job was running. Start it again to resume."
    ),
    "code": "JOB_INTERRUPTED",
}


async def _reclaim_stalled_jobs(app: FastAPI) -> int:
    """Mark jobs abandoned by a dead worker as failed with a resumable message.

    Runs at every worker start AND from a periodic sweep: the selecting
    predicate is the WORKER row's heartbeat, so a job stranded by a fast
    restart only becomes visible once that row ages past the threshold. It
    also selects every ``doing`` job whose worker row is gone entirely
    (``worker_id IS NULL``, no time condition) -- nothing else can ever reclaim
    those; the cost is that a live worker whose row was pruned during a >30s
    stall has its still-running job marked failed with a resumable message.
    Both services run this against the shared job table; the per-job guard
    below makes the resulting finish_job races benign.
    """
    from procrastinate.jobs import Status as ProcrastinateJobStatus  # noqa: PLC0415

    procrastinate_app = getattr(app.state, "procrastinate_app", None)
    if procrastinate_app is None:
        return 0
    db_pool = getattr(app.state, "db_pool", None)
    try:
        stalled = await procrastinate_app.job_manager.get_stalled_jobs(
            seconds_since_heartbeat=_STALLED_HEARTBEAT_SECONDS,
        )
    except Exception:  # noqa: BLE001 — reclamation must never block worker start
        logger.warning("Stalled-job reclamation failed; starting worker anyway", exc_info=True)
        return 0
    count = 0
    for job in stalled:
        try:
            await procrastinate_app.job_manager.finish_job(
                job, status=ProcrastinateJobStatus.FAILED, delete_job=False
            )
        except Exception:  # noqa: BLE001 — the sibling service's sweep may have won this job
            logger.debug("Job %s already reclaimed elsewhere; continuing", job.id, exc_info=True)
            continue
        # finish_job succeeded, so the job is reclaimed and counted regardless of
        # whether its interrupted-outcome row can be written. Surface that reason
        # through the channel the UI already reads; a failed write is best-effort
        # but must be reported, not lost as if it were the benign sweep race above.
        count += 1
        jarvis_job_id = (job.task_kwargs or {}).get("job_id")
        if jarvis_job_id and db_pool is not None:
            recorded = await ProcrastinateJobContextShim(
                job_id=str(jarvis_job_id), pool=db_pool
            ).record_terminal_outcome(error=_INTERRUPTED_JOB_ERROR, is_error=True)
            if not recorded:
                logger.warning(
                    "Job %s marked FAILED but its interrupted-outcome record could not be written",
                    job.id,
                )
    if count:
        logger.warning("Reclaimed %d job(s) abandoned by an interrupted worker", count)
    return count


async def _reclaim_stalled_jobs_forever(app: FastAPI) -> None:
    """Sweep for jobs abandoned by a dead worker until cancelled.

    :func:`_reclaim_stalled_jobs` swallows its own exceptions, so a transient
    database error cannot kill the loop.
    """
    while True:
        await asyncio.sleep(_RECLAIM_INTERVAL_SECONDS)
        await _reclaim_stalled_jobs(app)


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

    # The reclamation sweep issues the same UPDATE procrastinate_jobs writes a
    # restore must not observe, so it pauses with the worker loop.
    reclaim_task = getattr(app.state, "reclaim_task", None)
    if reclaim_task is not None:
        reclaim_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await reclaim_task
    app.state.reclaim_task = None


async def shutdown_procrastinate_worker(app: FastAPI) -> None:
    """Cancel the procrastinate worker task and close the connector."""
    task = getattr(app.state, "procrastinate_worker_task", None)
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    reclaim_task = getattr(app.state, "reclaim_task", None)
    if reclaim_task is not None:
        reclaim_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await reclaim_task
        app.state.reclaim_task = None

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

    Refreshes the API-key cache so a secret mount that settles after import is
    observed without a service restart.
    """
    refresh_api_key_cache()
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
            settings = get_jarvis_common_settings()
            configure_telemetry(
                service=config.service_name,
                enabled=settings.observability_enabled,
                otlp_endpoint=getattr(settings, "otel_exporter_otlp_traces_endpoint", None),
                timeout_ms=getattr(settings, "otel_export_timeout_ms", 5_000),
            )
            # The SDK owns final provider shutdown at process exit. Each app
            # lifecycle only gets a bounded flush, including failed startup.
            stack.callback(
                flush_telemetry,
                timeout_ms=getattr(settings, "otel_export_timeout_ms", 5_000),
            )
            database_url = build_database_url(
                user=settings.postgres_user,
                password_file=settings.postgres_password_file,
            )
            pool_kwargs = _resolve_db_pool_kwargs(config.db_pool_settings)
            db_pool = await asyncpg.create_pool(database_url, **pool_kwargs)
            stack.push_async_callback(db_pool.close)
            app.state.db_pool = db_pool
            app.state.database_url = database_url
            app.state.service_name = config.service_name

            # Runtime roles only inspect the completed schema. DDL belongs to
            # the one-shot migrator, which must have completed before APIs run.
            migration_check_started = time.monotonic()
            try:
                app.state.migration_check = await check_migrations(db_pool)
            except asyncpg.InsufficientPrivilegeError:
                duration_ms = int((time.monotonic() - migration_check_started) * 1000)
                logger.error(
                    "runtime schema check denied service=%s "
                    "capability=schema_integrity_read duration_ms=%d",
                    config.service_name,
                    duration_ms,
                )
                raise
            except Exception as exc:
                duration_ms = int((time.monotonic() - migration_check_started) * 1000)
                logger.error(
                    "runtime schema check failed service=%s error_type=%s duration_ms=%d",
                    config.service_name,
                    type(exc).__name__,
                    duration_ms,
                )
                raise
            app.state.migration_check_duration_ms = int(
                (time.monotonic() - migration_check_started) * 1000
            )

            http_kwargs = {
                **_HTTP_CLIENT_DEFAULTS,
                "transport": PinnedAsyncTransport(JARVIS_SERVICE_POLICY),
                **config.http_client_kwargs,
            }
            # A proxy would move DNS and TCP connection ownership outside the
            # pinned transport. It is not a supported route for service egress.
            http_kwargs["trust_env"] = False
            http_client = httpx.AsyncClient(**http_kwargs)
            stack.push_async_callback(http_client.aclose)
            app.state.http_client = http_client

            # Validate encrypted config rows after the read-only schema check so
            # a completed one-shot migration never produces a misleading
            # fresh-schema warning.
            try:
                await validate_encrypted_config_rows(db_pool)
            except (asyncpg.UndefinedTableError, asyncpg.UndefinedColumnError):
                logger.warning(
                    "validate_encrypted_config_rows skipped: user_config table not available"
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
    X-Forwarded-For. The rate limiter uses both values to distinguish the
    transport peer from forwarded client metadata.
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
    app.add_middleware(SlowAPIASGIMiddleware)

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
    # from X-Forwarded-For for rate-limit keying.
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
        # Bind jobs to the exact runtime-role DSN used for the lifespan pool.
        database_url = getattr(app.state, "database_url", None) or build_database_url()
        procrastinate_app.connector = AiopgConnector(dsn=database_url)
        procrastinate_app.job_manager.connector = procrastinate_app.connector
        await procrastinate_app.open_async()

        set_dependencies(app.state.db_pool, app.state.http_client)

        app.state.procrastinate_app = procrastinate_app
        await _reclaim_stalled_jobs(app)
        _start_worker_task(app, queues)
        app.state.reclaim_task = asyncio.create_task(
            _reclaim_stalled_jobs_forever(app), name="reclaim_stalled_jobs"
        )

    return _hook


async def _resume_after_maintenance(app: FastAPI, queues: list[str]) -> None:
    """Verify a restored schema before resuming background writers.

    Ordered so writers never observe a stale schema or stale DB-derived cache:
    verify the one-shot migrator's completed schema first, drop the in-process
    caches whose backing ``user_config`` rows a restore may have rolled back, then
    restart the worker loop. Only reached when the secrets were NOT rotated — the
    watcher step self-restarts BEFORE calling this on a rotation, because the DB
    ops here would auth-fail against the rebound role until that restart. Kept a
    discrete function so the watcher can gate it cleanly.
    """
    app.state.migration_check = await check_migrations(app.state.db_pool)
    invalidate_api_key_login_cache()
    invalidate_effective_num_ctx_cache()
    await _reclaim_stalled_jobs(app)
    _start_worker_task(app, queues)
    app.state.reclaim_task = asyncio.create_task(
        _reclaim_stalled_jobs_forever(app), name="reclaim_stalled_jobs"
    )
    logger.info("maintenance: reconciled schema and resumed writers")


def _trigger_secrets_rotation_restart() -> None:
    """Signal our own process to exit so ``restart: unless-stopped`` reloads secrets.

    Compose file-secrets are per-inode bind mounts read once at process start, so an
    off-host restore's role rebind + ./secrets rotation is only picked up by a full
    container exit + revive. SIGTERM lets uvicorn drain cleanly (exit 143 still
    restarts). Isolated so the watcher's decision path stays unit-testable without
    killing the test process.
    """
    logger.warning("maintenance: secrets rotated; restarting to reload updated secrets")
    os.kill(os.getpid(), signal.SIGTERM)


async def _maintenance_watcher_step(
    app: FastAPI, queues: list[str], *, was_active: bool, started_at: float | None = None
) -> bool:
    """Run one watcher poll; return the current maintenance-active state.

    ``inactive→active`` cancels the worker loop (queue hygiene during a restore);
    ``active→inactive`` self-restarts FIRST when a restore rotated the secrets after
    this process started (see below), else reconciles the schema and resumes writers.
    """
    now_active = maintenance_active()
    if now_active and not was_active:
        await _pause_worker_task(app)
        logger.info("maintenance: paused background writers")
    elif was_active and not now_active:
        # A cross-host (inbox) restore rebinds the postgres role and rotates
        # ./secrets while this process still holds the pre-rotation password in
        # its pool, so the runtime schema check's ``pool.acquire`` would
        # auth-fail until we restart. Signal the restart BEFORE reconciling — the
        # revived process reloads the rotated secret and checks its schema at boot;
        # sequencing it after the resume would deadlock (the failing acquire raises
        # before the restart could fire). A same-host restore (no rotation) has a
        # still-valid pool, so it falls through to the in-place resume.
        if started_at is not None and secrets_rotated_since(started_at):
            _trigger_secrets_rotation_restart()
            return now_active
        await _resume_after_maintenance(app, queues)
    return now_active


async def _maintenance_watcher_loop(
    app: FastAPI, queues: list[str], poll_interval_s: float, started_at: float | None = None
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
            was_active = await _maintenance_watcher_step(
                app, queues, was_active=was_active, started_at=started_at
            )
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

    ``started_at`` is captured here (module-import time for each service) so a
    secrets-rotation marker newer than this boot triggers exactly one self-restart;
    the restarted process captures a fresh ``started_at`` and does not re-exit.
    """
    started_at = time.time()

    async def _hook(app: FastAPI) -> None:
        app.state.maintenance_watcher_task = asyncio.create_task(
            _maintenance_watcher_loop(app, queues, poll_interval_s, started_at),
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
