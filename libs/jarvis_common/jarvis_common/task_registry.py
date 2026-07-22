"""Shared Procrastinate application, task registry, and dispatch adapter.

Services provide runtime dependencies and register their async handlers before
workers start. Generated tasks adapt those handlers to Procrastinate, expose a
read-only kind-to-task mapping, and retry outbound-quarantine failures without
marking the job complete. The connector is configured by service lifespan code;
importing this module does not open a database connection.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

import procrastinate
from procrastinate.contrib.aiopg import AiopgConnector

from jarvis_common._ctx_shim import make_ctx_shim
from jarvis_common.event_log import log_event
from jarvis_common.jobs import JobError
from jarvis_common.logging_config import correlation_id_var
from jarvis_common.maintenance import OutboundEgressBlockedError, skip_for_maintenance
from jarvis_common.settings import get_jobs_settings

if TYPE_CHECKING:
    import asyncpg
    import httpx

logger = logging.getLogger(__name__)
_RESTORE_BLOCK_RETRY = procrastinate.RetryStrategy(
    max_attempts=None,
    wait=30,
    retry_exceptions={OutboundEgressBlockedError},
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
        as successfully completed while egress remains prohibited. Generated
        task objects are stored in ``kind_to_task`` for API dispatch.
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
                if skip_for_maintenance(f"task {_task_kind}"):
                    raise OutboundEgressBlockedError(
                        "background task is blocked by temporary restore state"
                    )
                return await _run_legacy_handler(
                    context,
                    payload,
                    _h,
                    dependencies=self.require_dependencies(),
                )

            self.kind_to_task[kind] = _task_wrapper

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


async def _run_legacy_handler(
    context: procrastinate.JobContext,
    payload: dict[str, Any],
    handler: Callable[
        [asyncpg.Pool, httpx.AsyncClient, dict[str, Any], Any],
        Awaitable[dict[str, Any]],
    ],
    *,
    dependencies: TaskDependencies | None = None,
) -> dict[str, Any]:
    """Run a legacy handler and persist terminal Procrastinate outcome payloads."""
    import uuid  # noqa: PLC0415

    if dependencies is None:
        pool, http_client = _require_dependencies()
    else:
        pool, http_client = dependencies.pool, dependencies.http_client
    ctx = make_ctx_shim(context, pool=pool)

    # Derive task_kind and job_id for structured event emission.
    task_kind: str = getattr(getattr(context, "job", None), "task_name", "") or ""
    job_id: str = ctx.job_id  # JARVIS UUID (or procrastinate bigint as str)

    corr = uuid.uuid4()
    token = correlation_id_var.set(corr)
    try:
        await log_event(
            pool=pool,
            level="info",
            category="job",
            source=task_kind,
            message="started",
            context={"job_id": job_id, "task_kind": task_kind},
            correlation_id=corr,
        )
        try:
            result = await handler(pool, http_client, payload, ctx)
        except Exception as exc:
            # CancelledError (a BaseException) propagates without persistence:
            # a cancel is not a job failure and must not poison retry state.
            await ctx.record_terminal_outcome(error=_terminal_error_payload(exc), is_error=True)
            await log_event(
                pool=pool,
                level="error",
                category="job",
                source=task_kind,
                message="failed",
                context={"job_id": job_id, "task_kind": task_kind, "error": repr(exc)[:500]},
                correlation_id=corr,
            )
            raise
        await ctx.record_terminal_outcome(result=result, is_error=False)
        await log_event(
            pool=pool,
            level="info",
            category="job",
            source=task_kind,
            message="finished",
            context={"job_id": job_id, "task_kind": task_kind, "result": str(result)[:500]},
            correlation_id=corr,
        )
        return result
    finally:
        correlation_id_var.reset(token)


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
    "KIND_TO_TASK",
]
