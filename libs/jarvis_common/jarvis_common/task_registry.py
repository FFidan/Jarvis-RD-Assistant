"""Shared Procrastinate application, task registry, and dispatch adapter.

Services provide runtime dependencies and register their async handlers before
workers start. Generated tasks adapt those handlers to Procrastinate, expose a
read-only kind-to-task mapping, and retry outbound-quarantine failures without
marking the job complete. The connector is configured by service lifespan code;
importing this module does not open a database connection.

Distinct responsibility from ``jobs`` (queue-backbone types, DB reads, SSE
streaming, and the kind-to-queue ownership map this module consumes via
``queue_for_kind``) and from ``jobs_router`` (the per-service REST factory
built on top of ``jobs``): this module is where handlers are registered so
Procrastinate can dispatch to them, not where jobs are queried or exposed.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from time import perf_counter
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol

import procrastinate
from opentelemetry.trace import SpanKind
from procrastinate.contrib.aiopg import AiopgConnector

from jarvis_common.event_log import log_event
from jarvis_common.job_context import make_ctx_shim
from jarvis_common.jobs import JobError, queue_for_kind
from jarvis_common.logging_config import correlation_id_var
from jarvis_common.maintenance import (
    OutboundEgressBlockedError,
    OutboundQuarantineBlockedError,
    maintenance_skip_reason,
)
from jarvis_common.settings import get_jobs_settings
from jarvis_common.telemetry import (
    capture_task_context,
    pop_task_context,
    record_worker,
    restored_span,
)

if TYPE_CHECKING:
    import asyncpg
    import httpx

logger = logging.getLogger(__name__)
_RESTORE_BLOCK_RETRY = procrastinate.RetryStrategy(
    max_attempts=None,
    wait=30,
    retry_exceptions={OutboundEgressBlockedError},
)
# Quarantine waits for a person, so its retries are bounded: 120 attempts at the
# 30-second wait above is roughly an hour, after which the job fails visibly
# instead of retrying silently forever. Restore maintenance keeps its unlimited
# budget because it clears itself.
#
# The counter is the job's own attempt total, which also advances while restore
# maintenance is active, so a job that waited out a long restore can reach the
# bound on its first quarantine-classified attempt. The message therefore states
# that retrying stopped, not how long it went on for.
_QUARANTINE_MAX_ATTEMPTS = 120
_QUARANTINE_GAVE_UP_MESSAGE = (
    "Outbound integrations are quarantined after a restore; acknowledge the restore"
    " to resume. This job has stopped retrying."
)


@dataclass(frozen=True, slots=True)
class TaskDependencies:
    """Runtime collaborators required by Procrastinate task dispatchers.

    Parameters
    ----------
    pool : asyncpg.Pool
        Async PostgreSQL pool shared with the FastAPI service instance.
    http_client : httpx.AsyncClient
        Shared HTTP client owned by the service lifespan.

    """

    pool: asyncpg.Pool
    http_client: httpx.AsyncClient


class _LegacyHandler(Protocol):
    """Callable contract retained by legacy job adapters."""

    def __call__(
        self,
        pool: asyncpg.Pool,
        http_client: httpx.AsyncClient,
        payload: dict[str, Any],
        ctx: Any,
    ) -> Awaitable[dict[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class _HandlerExecution:
    """Values shared across one legacy handler lifecycle."""

    dependencies: TaskDependencies
    payload: dict[str, Any]
    task_kind: str
    job_id: str
    correlation_id: uuid.UUID
    started: float


# ---------------------------------------------------------------------------
# App + connector
# ---------------------------------------------------------------------------
#
# The connector is created with no explicit DSN — services must call
# ``app.with_connector(...)`` or open the app inside an existing aiopg pool
# during their lifespan startup. This keeps the module import-side-effect-free
# (no DB connection at import time).
app = procrastinate.App(connector=AiopgConnector())


class TaskRegistry:
    """Register handler-backed tasks for one Procrastinate application.

    Parameters
    ----------
    procrastinate_app : procrastinate.App
        Application that owns the registered tasks.
    task_map : dict[str, Any] | None
        Mutable mapping that receives each registered task by kind.
    """

    def __init__(
        self,
        procrastinate_app: procrastinate.App,
        *,
        task_map: dict[str, Any] | None = None,
    ) -> None:
        """Bind a procrastinate App and an optional pre-populated kind→task mapping."""
        self.app = procrastinate_app
        self.kind_to_task = task_map if task_map is not None else {}
        self._dependencies: TaskDependencies | None = None

    def set_dependencies(
        self,
        pool: asyncpg.Pool,
        http_client: httpx.AsyncClient,
    ) -> None:
        """Set the runtime collaborators used by task handlers.

        Parameters
        ----------
        pool : asyncpg.Pool
            Database pool shared with task handlers.
        http_client : httpx.AsyncClient
            HTTP client shared with task handlers.
        """
        self._dependencies = TaskDependencies(pool=pool, http_client=http_client)

    def require_dependencies(self) -> TaskDependencies:
        """Return the configured runtime collaborators.

        Returns
        -------
        TaskDependencies
            Database and HTTP clients used by task handlers.

        Raises
        ------
        RuntimeError
            If dependencies have not been configured.
        """
        if self._dependencies is None:
            raise RuntimeError(
                "task_registry: set_dependencies(pool, http_client) must be called "
                "during service lifespan startup before any procrastinate task runs."
            )
        return self._dependencies

    def register_tasks(
        self,
        mapping: dict[str, Callable[..., Awaitable[dict[str, Any]]]],
        queue: str,
    ) -> None:
        """Register handlers as restore-aware tasks on this registry's app.

        Parameters
        ----------
        mapping : dict[str, Callable]
            Task kind to asynchronous handler mapping.
        queue : str
            Procrastinate queue assigned to every generated task.

        Notes
        -----
        Each wrapper checks maintenance and outbound quarantine before resolving
        runtime dependencies. ``OutboundEgressBlockedError`` alone receives an
        unlimited retry budget with a 30-second wait; the task is never treated
        as successfully completed while egress remains prohibited. Quarantine
        raises a subclass of it, so it shares that budget until roughly an hour
        of attempts has passed and the wrapper fails the job with a message
        naming the acknowledgement that would release it. Generated task objects
        are stored in ``kind_to_task`` for API dispatch.
        """
        for kind, handler in mapping.items():
            # Capture handler in default arg to avoid late-binding closure bugs.
            @self.app.task(
                name=kind,
                queue=queue,
                pass_context=True,
                retry=_RESTORE_BLOCK_RETRY,
            )
            async def _task_wrapper(
                context: procrastinate.JobContext,
                _h: Callable[..., Awaitable[dict[str, Any]]] = handler,
                _task_kind: str = kind,
                **payload: Any,
            ) -> dict[str, Any]:
                reason = maintenance_skip_reason(f"task {_task_kind}")
                if reason == "quarantine":
                    if context.job.attempts >= _QUARANTINE_MAX_ATTEMPTS:
                        gave_up = JobError(_QUARANTINE_GAVE_UP_MESSAGE)
                        await _record_terminal_error(
                            context, self.require_dependencies().pool, gave_up
                        )
                        raise gave_up
                    raise OutboundQuarantineBlockedError(
                        "background task is blocked by temporary restore state"
                    )
                if reason is not None:
                    raise OutboundEgressBlockedError(
                        "background task is blocked by temporary restore state"
                    )
                return await _run_legacy_handler(
                    context,
                    payload,
                    _h,
                    dependencies=self.require_dependencies(),
                )

            self.kind_to_task[kind] = _TaskEnqueueProxy(_task_wrapper)

        logger.debug(
            "register_tasks: registered %d tasks on queue=%r: %s",
            len(mapping),
            queue,
            sorted(mapping.keys()),
        )


_TASK_MAP: dict[str, Any] = {}
KIND_TO_TASK: MappingProxyType[str, Any] = MappingProxyType(_TASK_MAP)
_DEFAULT_REGISTRY = TaskRegistry(app, task_map=_TASK_MAP)

# Module-level adapters delegate to the default registry.
_pool: asyncpg.Pool | None = None
_http_client: httpx.AsyncClient | None = None


class _TaskEnqueueProxy:
    """Add propagation metadata without changing Procrastinate registration."""

    def __init__(self, task: Any) -> None:
        self._task = task

    async def defer_async(self, **payload: Any) -> Any:
        return await self._task.defer_async(**capture_task_context(payload))

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """Delegate direct task execution without treating it as an enqueue."""
        return await self._task(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._task, name)


def set_dependencies(
    pool: asyncpg.Pool,
    http_client: httpx.AsyncClient,
) -> None:
    """Set the runtime collaborators used by the default task registry.

    Parameters
    ----------
    pool : asyncpg.Pool
        Database pool shared with task handlers.
    http_client : httpx.AsyncClient
        HTTP client shared with task handlers.

    Notes
    -----
    Services configure these dependencies before starting their worker. A later
    call replaces both collaborators.
    """
    global _pool, _http_client
    _pool = pool
    _http_client = http_client
    _DEFAULT_REGISTRY.set_dependencies(pool, http_client)


def _require_dependencies() -> tuple[asyncpg.Pool, httpx.AsyncClient]:
    """Return (pool, http_client) or raise if ``set_dependencies`` was never called."""
    if _pool is not None and _http_client is not None:
        return _pool, _http_client
    deps = _DEFAULT_REGISTRY.require_dependencies()
    return deps.pool, deps.http_client


def _terminal_error_payload(exc: BaseException) -> dict[str, Any]:
    """Return a JSON-safe terminal job error payload."""
    if isinstance(exc, JobError):
        payload: dict[str, Any] = {"message": str(exc) or exc.__class__.__name__}
        if exc.action_link is not None:
            payload["action_link"] = exc.action_link
        return payload
    return {"message": "Job failed", "code": "JOB_FAILED"}


async def _record_terminal_error(
    context: procrastinate.JobContext,
    pool: asyncpg.Pool,
    error: BaseException,
) -> None:
    """Persist a terminal outcome for a failure raised outside the handler path.

    ``_run_handler_with_context`` is the only other writer of terminal outcomes,
    so a job that never reaches the handler fails with an empty error payload
    and its message never reaches the user. Routing such a failure through
    :func:`_run_legacy_handler` instead would emit a spurious ``started`` event
    and consume the queued trace carrier, corrupting the trace of the attempt
    that actually ran.
    """
    ctx = make_ctx_shim(context, pool=pool)
    await ctx.record_terminal_outcome(error=_terminal_error_payload(error), is_error=True)


async def _run_legacy_handler(
    context: procrastinate.JobContext,
    payload: dict[str, Any],
    handler: _LegacyHandler,
    *,
    dependencies: TaskDependencies | None = None,
) -> dict[str, Any]:
    """Run a legacy handler and persist terminal Procrastinate outcome payloads."""
    if dependencies is None:
        pool, http_client = _require_dependencies()
        dependencies = TaskDependencies(pool=pool, http_client=http_client)
    ctx = make_ctx_shim(context, pool=dependencies.pool)

    # Derive task_kind and job_id for structured event emission.
    task_kind: str = getattr(getattr(context, "job", None), "task_name", "") or ""
    job_id: str = ctx.job_id  # JARVIS UUID (or procrastinate bigint as str)

    carrier, corr = pop_task_context(payload)
    token = correlation_id_var.set(corr)
    started = perf_counter()
    try:
        with restored_span(
            carrier=carrier,
            service="worker",
            name="job.execute",
            kind=SpanKind.CONSUMER,
        ):
            return await _run_handler_with_context(
                _HandlerExecution(
                    dependencies=dependencies,
                    payload=payload,
                    task_kind=task_kind,
                    job_id=job_id,
                    correlation_id=corr,
                    started=started,
                ),
                ctx,
                handler,
            )
    finally:
        correlation_id_var.reset(token)


async def _run_handler_with_context(
    execution: _HandlerExecution,
    ctx: Any,
    handler: _LegacyHandler,
) -> dict[str, Any]:
    """Emit lifecycle events while one restored worker span is active."""
    await log_event(
        pool=execution.dependencies.pool,
        level="info",
        category="job",
        source=execution.task_kind,
        message="started",
        context={"job_id": execution.job_id, "task_kind": execution.task_kind},
        correlation_id=execution.correlation_id,
    )
    try:
        result = await handler(
            execution.dependencies.pool,
            execution.dependencies.http_client,
            execution.payload,
            ctx,
        )
    except Exception as exc:
        # CancelledError (a BaseException) propagates without persistence:
        # a cancel is not a job failure and must not poison retry state.
        await ctx.record_terminal_outcome(error=_terminal_error_payload(exc), is_error=True)
        await log_event(
            pool=execution.dependencies.pool,
            level="error",
            category="job",
            source=execution.task_kind,
            message="failed",
            context={
                "job_id": execution.job_id,
                "task_kind": execution.task_kind,
                "error": repr(exc)[:500],
            },
            correlation_id=execution.correlation_id,
        )
        record_worker(
            service="worker",
            outcome="error",
            duration_s=perf_counter() - execution.started,
            task_kind=execution.task_kind,
        )
        raise
    await ctx.record_terminal_outcome(result=result, is_error=False)
    await log_event(
        pool=execution.dependencies.pool,
        level="info",
        category="job",
        source=execution.task_kind,
        message="finished",
        context={
            "job_id": execution.job_id,
            "task_kind": execution.task_kind,
            "result": str(result)[:500],
        },
        correlation_id=execution.correlation_id,
    )
    record_worker(
        service="worker",
        outcome="success",
        duration_s=perf_counter() - execution.started,
        task_kind=execution.task_kind,
    )
    return result


# ---------------------------------------------------------------------------
# Registration API
# ---------------------------------------------------------------------------
#
# Services call ``register_tasks`` during startup, before the worker starts.
# ``TaskRegistry.register_tasks`` constructs tasks and configures retries; the
# module-level function delegates to the default registry.
#
# The decorator always adds noop.test to the Procrastinate app. KIND_TO_TASK only
# exposes it when JARVIS_ENABLE_TEST_JOBS=1.


def register_tasks(
    procrastinate_app: procrastinate.App,
    mapping: dict[str, Callable[..., Awaitable[dict[str, Any]]]],
    queue: str,
) -> None:
    """Register handlers through the default restore-aware task registry.

    Parameters
    ----------
    procrastinate_app : procrastinate.App
        The shared procrastinate ``App`` instance from this module.
    mapping : dict[str, Callable]
        Task kind to async handler mapping.
    queue : str
        Procrastinate queue assigned to every generated task.

    Notes
    -----
    Call this before the worker starts. The generated tasks use the maintenance
    and quarantine behavior documented by :meth:`TaskRegistry.register_tasks`,
    including retrying ``OutboundEgressBlockedError`` without completing the job.
    """
    if procrastinate_app is _DEFAULT_REGISTRY.app:
        registry = _DEFAULT_REGISTRY
    else:
        registry = TaskRegistry(procrastinate_app, task_map=_TASK_MAP)
        if _DEFAULT_REGISTRY._dependencies is not None:
            registry.set_dependencies(
                pool=_DEFAULT_REGISTRY._dependencies.pool,
                http_client=_DEFAULT_REGISTRY._dependencies.http_client,
            )
    registry.register_tasks(mapping, queue)


def register_service_tasks(
    procrastinate_app: procrastinate.App,
    mapping: dict[str, Callable[..., Awaitable[dict[str, Any]]]],
    *,
    service_label: str,
) -> None:
    """Register one service's kind→handler mapping with fail-fast startup guards.

    Shared entrypoint for each service's ``register_<service>_tasks``. Derives the
    single consume-queue from the handled kinds (via :func:`queue_for_kind`),
    registers the handlers, then verifies every kind landed in ``app.tasks``.

    Parameters
    ----------
    procrastinate_app : procrastinate.App
        Application the tasks are registered on. Must be called during lifespan
        startup, before the worker starts.
    mapping : dict[str, Callable]
        Task kind to async handler mapping for a single service.
    service_label : str
        Caller name threaded into both guard errors (e.g.
        ``"register_paper_ingestion_tasks"``).

    Raises
    ------
    RuntimeError
        If the handled kinds span more than one owner queue, or if any kind is
        missing from ``app.tasks`` after registration (fail-fast at startup,
        ``-O``-proof — a bare assert would be stripped under ``python -O``).
    """
    queues = {queue_for_kind(kind) for kind in mapping}
    if len(queues) != 1:
        raise RuntimeError(
            f"{service_label}: KIND_TO_HANDLER spans multiple "
            f"owner queues {sorted(queues)}; each kind must map to one service"
        )
    (queue,) = queues
    register_tasks(procrastinate_app, mapping=mapping, queue=queue)

    missing = [kind for kind in mapping if kind not in procrastinate_app.tasks]
    if missing:
        raise RuntimeError(f"{service_label}: failed to register kinds: {missing}")


# ---------------------------------------------------------------------------
# Test-only: noop.test
# ---------------------------------------------------------------------------
#
# The decorator always registers noop.test with Procrastinate. The task map only
# exposes it when JARVIS_ENABLE_TEST_JOBS=1. Service registrars check the setting
# again because environment configuration may be loaded after this module.


@app.task(name="noop.test", queue="paper_ingestion", pass_context=True)
async def noop_task(context: procrastinate.JobContext, **payload: Any) -> dict[str, Any]:
    """Return the supplied payload when test jobs are enabled.

    Parameters
    ----------
    context : procrastinate.JobContext
        Procrastinate execution context. The task does not modify it.
    **payload : Any
        Values to echo in the result.

    Returns
    -------
    dict[str, Any]
        Success status and the supplied payload.

    Raises
    ------
    RuntimeError
        If ``JARVIS_ENABLE_TEST_JOBS`` is not enabled.
    """
    if not get_jobs_settings().test_jobs_enabled:
        raise RuntimeError("noop.test invoked but JARVIS_ENABLE_TEST_JOBS is unset")
    return {"ok": True, "echo": payload}


# Add noop.test to the dispatch map only when test jobs are enabled.
if get_jobs_settings().test_jobs_enabled:
    _TASK_MAP["noop.test"] = noop_task

# ---------------------------------------------------------------------------
# Public exports
# ---------------------------------------------------------------------------

__all__ = [
    "app",
    "TaskDependencies",
    "TaskRegistry",
    "set_dependencies",
    "register_tasks",
    "register_service_tasks",
    "KIND_TO_TASK",
]
