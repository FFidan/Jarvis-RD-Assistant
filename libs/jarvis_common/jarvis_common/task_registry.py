"""Procrastinate task registry — owns the App factory + registration API.

B.4 cutover is complete as of 2026-05-03 (see ``docs/plans/2026-05-03-marathon-status.md``
for the cutover record). The Procrastinate worker is wired into both
``paper_ingestion`` and ``learning_engine`` service lifespans via
``app.run_worker_async()``. All enqueue paths use these tasks; the legacy
``worker_loop`` has been removed.

W4-1 (Wave 2.2) — dependency inversion:
    jarvis_common owns the procrastinate ``App`` factory and exposes
    ``register_tasks(app, mapping, queue)`` to receive kind→handler dicts
    from each service. Each service registers its own tasks during lifespan
    startup (before the worker is started) via its ``_task_register`` module:

        paper_ingestion/_task_register.py  → 18 kinds on "paper_ingestion" queue
        learning_engine/_task_register.py  → 2 kinds on "learning_engine" queue

    jarvis_common no longer imports paper_ingestion or learning_engine.

Each task body is a thin dispatcher that calls the existing legacy handler
via ``ProcrastinateJobContextShim`` (see ``_ctx_shim.py``). The legacy
handler signature is::

    async def handler(
        pool: asyncpg.Pool,
        http_client: httpx.AsyncClient,
        payload: dict[str, Any],
        ctx: JobContext,
    ) -> dict[str, Any]

so each task forwards exactly that.

KIND_TO_TASK is populated at runtime as services call ``register_tasks``.
It is used by ``jobs_router.create_job`` for procrastinate dispatch.

Connector choice: ``procrastinate.contrib.aiopg.AiopgConnector`` (matches
``procrastinate[aiopg]>=0.49`` declared in root ``pyproject.toml`` and in
``services/*/requirements.txt``). Note that the top-level alias
``procrastinate.AiopgConnector`` was removed in 3.x — the import path is
``procrastinate.contrib.aiopg``.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import procrastinate
from procrastinate.contrib.aiopg import AiopgConnector

from jarvis_common._ctx_shim import make_ctx_shim
from jarvis_common.jobs import JobError
from jarvis_common.settings import get_jobs_settings

if TYPE_CHECKING:
    import asyncpg
    import httpx

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# App + connector
# ---------------------------------------------------------------------------
#
# The connector is created with no explicit DSN — services must call
# ``app.with_connector(...)`` or open the app inside an existing aiopg pool
# during their lifespan startup. This keeps the module import-side-effect-free
# (no DB connection at import time).
app = procrastinate.App(connector=AiopgConnector())


# ---------------------------------------------------------------------------
# Module-level dependency injection
# ---------------------------------------------------------------------------
#
# The legacy handlers expect ``(pool, http_client, payload, ctx)``. Procrastinate
# tasks only receive ``(context, **payload)``. To bridge the gap, services set
# the pool + http_client refs during lifespan startup *before* the worker is
# started; each task body reads them from module scope at dispatch time.
#
# This is identical in spirit to how the legacy ``run_job`` (jobs.py:584)
# threads pool + http_client through the worker loop, just hoisted to module
# scope instead of being a function argument.
_pool: asyncpg.Pool | None = None
_http_client: httpx.AsyncClient | None = None


def set_dependencies(
    pool: asyncpg.Pool,
    http_client: httpx.AsyncClient,
) -> None:
    """Set the pool + http_client refs read by every task dispatcher.

    Must be called by each service's lifespan BEFORE the procrastinate worker
    is started. Calling it more than once is allowed (last writer wins).
    Calling it is NOT required for tasks to be registered — registration
    happens at service startup via ``register_tasks``.
    """
    global _pool, _http_client
    _pool = pool
    _http_client = http_client


def _require_dependencies() -> tuple[asyncpg.Pool, httpx.AsyncClient]:
    """Return (pool, http_client) or raise if ``set_dependencies`` was never called."""
    if _pool is None or _http_client is None:
        raise RuntimeError(
            "task_registry: set_dependencies(pool, http_client) must be called "
            "during service lifespan startup before any procrastinate task runs."
        )
    return _pool, _http_client


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
) -> dict[str, Any]:
    """Run a legacy handler and persist terminal Procrastinate outcome payloads."""
    import asyncio  # noqa: PLC0415

    pool, http_client = _require_dependencies()
    ctx = make_ctx_shim(context, pool=pool)
    try:
        result = await handler(pool, http_client, payload, ctx)
    except BaseException as exc:
        if isinstance(exc, asyncio.CancelledError):
            await ctx.record_terminal_outcome(error=_terminal_error_payload(exc), is_error=True)
        elif isinstance(exc, Exception):
            await ctx.record_terminal_outcome(error=_terminal_error_payload(exc), is_error=True)
        raise
    await ctx.record_terminal_outcome(result=result, is_error=False)
    return result


# ---------------------------------------------------------------------------
# Registration API
# ---------------------------------------------------------------------------
#
# Services call ``register_tasks`` during lifespan startup (BEFORE the worker
# starts) to declare their kind→handler mapping.  For each entry, a
# ``@app.task(name=kind, queue=queue, pass_context=True)`` wrapper is
# registered on the shared ``app`` and the task object is inserted into
# ``KIND_TO_TASK`` for ``jobs_router.create_job`` to dispatch.
#
# The noop.test task is special: it is registered unconditionally so the
# procrastinate App always carries it (test infrastructure needs it), but
# it is only added to KIND_TO_TASK when JARVIS_ENABLE_TEST_JOBS=1, preventing
# production exposure via the create_job API.

# Populated at runtime by ``register_tasks`` calls from service startup hooks.
KIND_TO_TASK: dict[str, Any] = {}


def register_tasks(
    procrastinate_app: procrastinate.App,
    mapping: dict[str, Callable[..., Awaitable[dict[str, Any]]]],
    queue: str,
) -> None:
    """Register kind→handler entries as procrastinate tasks on ``procrastinate_app``.

    For each ``(kind, handler)`` pair:
      - Registers an ``@app.task(name=kind, queue=queue, pass_context=True)`` wrapper.
      - Inserts the resulting task object into the module-level ``KIND_TO_TASK``
        dict so ``jobs_router.create_job`` can dispatch via ``task.defer_async``.

    Must be called BEFORE ``procrastinate_app.run_worker_async()`` is started.

    Args:
        procrastinate_app: The shared procrastinate ``App`` instance from this module.
        mapping: Dict mapping JARVIS job kind strings to legacy handler callables.
        queue: The procrastinate queue name for this service (e.g. "paper_ingestion").
    """
    for kind, handler in mapping.items():
        # Capture handler in default arg to avoid late-binding closure bug.
        @procrastinate_app.task(name=kind, queue=queue, pass_context=True)
        async def _task_wrapper(
            context: procrastinate.JobContext,
            _h: Callable[..., Awaitable[dict[str, Any]]] = handler,
            **payload: Any,
        ) -> dict[str, Any]:
            return await _run_legacy_handler(context, payload, _h)

        KIND_TO_TASK[kind] = _task_wrapper

    logger.debug(
        "register_tasks: registered %d tasks on queue=%r: %s",
        len(mapping),
        queue,
        sorted(mapping.keys()),
    )


# ---------------------------------------------------------------------------
# Test-only: noop.test
# ---------------------------------------------------------------------------
#
# Registered unconditionally so test infrastructure can always use it.
# Only added to KIND_TO_TASK (the create_job dispatch surface) when
# JARVIS_ENABLE_TEST_JOBS=1, so production envs are unaffected.


@app.task(name="noop.test", queue="paper_ingestion", pass_context=True)
async def noop_task(context: procrastinate.JobContext, **payload: Any) -> dict[str, Any]:
    """No-op smoke-test task. Gated on JARVIS_ENABLE_TEST_JOBS=1."""
    if not get_jobs_settings().test_jobs_enabled:
        raise RuntimeError("noop.test invoked but JARVIS_ENABLE_TEST_JOBS is unset")
    return {"ok": True, "echo": payload}


# Gate noop.test on the test-jobs toggle so production envs are unaffected.
if get_jobs_settings().test_jobs_enabled:
    KIND_TO_TASK["noop.test"] = noop_task

# ---------------------------------------------------------------------------
# Public exports
# ---------------------------------------------------------------------------

__all__ = [
    "app",
    "set_dependencies",
    "register_tasks",
    "KIND_TO_TASK",
]
