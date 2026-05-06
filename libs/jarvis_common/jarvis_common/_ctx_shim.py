"""JobContext-compatible shim bridging procrastinate ↔ legacy ``jarvis_common.jobs.JobContext``.

The legacy 19 ``@job_handler``-decorated functions all expect a
``jarvis_common.jobs.JobContext`` (a ``@dataclass`` with ``job_id: str``,
``async update_progress(progress, message=None)``, ``async is_cancelled()``).
Procrastinate hands tasks its own ``procrastinate.JobContext`` — a different
object with ``context.job.id``, ``context.should_abort()``, etc.

This module provides a thin adapter so the legacy handlers can be invoked
unchanged from inside a procrastinate task body:

- ``update_progress``: UPSERTs into the ``job_progress`` table (migration
  054). Silently degrades to a no-op when no pool is supplied or the table
  is missing — older DBs without migration 054 still run, they just don't
  surface progress.
- ``is_cancelled``: bridges to procrastinate's ``should_abort()`` so
  abort-requested propagates to handler bodies.
- ``job_id``: prefers the JARVIS UUID stored in ``task_kwargs['job_id']``
  (set by every enqueue path) and falls back to ``str(procrastinate.job.id)``
  (the bigint id) only when the kwarg is missing.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    # Imported lazily — procrastinate is in the dependency graph but importing
    # it eagerly would couple every consumer of jarvis_common to libpq being
    # available, which is undesirable for unit tests.
    import asyncpg
    from procrastinate import JobContext as _ProcrastinateJobContext


class ProcrastinateJobContextShim:
    """Adapter exposing the legacy ``jarvis_common.jobs.JobContext`` surface.

    The legacy contract (see ``libs/jarvis_common/jarvis_common/jobs.py:464``):
        - ``job_id: str``                         — attribute
        - ``async update_progress(progress, message=None) -> None``
        - ``async is_cancelled() -> bool``
    """

    __slots__ = ("job_id", "_procrastinate_ctx", "_pool")

    def __init__(
        self,
        *,
        job_id: str,
        procrastinate_ctx: Any | None = None,
        pool: asyncpg.Pool | None = None,
    ) -> None:
        self.job_id = job_id
        self._procrastinate_ctx = procrastinate_ctx
        self._pool = pool

    async def update_progress(
        self,
        progress: float,
        message: str | None = None,
    ) -> None:
        """UPSERT progress into the ``job_progress`` table.

        Silently degrades to a debug log when no pool is configured (unit
        tests) or when the ``job_progress`` table doesn't exist (older DB
        without migration 054 applied).
        """
        if self._pool is None or not self.job_id:
            logger.debug(
                "ctx_shim.update_progress no-op (pool=%s, job_id=%r) progress=%.2f msg=%s",
                self._pool,
                self.job_id,
                progress,
                message,
            )
            return
        try:
            await self._pool.execute(
                """
                INSERT INTO job_progress (jarvis_job_id, progress, message, updated_at)
                VALUES ($1, $2, $3, NOW())
                ON CONFLICT (jarvis_job_id) DO UPDATE
                  SET progress   = EXCLUDED.progress,
                      message    = EXCLUDED.message,
                      updated_at = EXCLUDED.updated_at
                """,
                self.job_id,
                float(progress),
                message,
            )
        except Exception:  # noqa: BLE001 — never let progress reporting kill the job
            logger.debug(
                "ctx_shim.update_progress UPSERT failed for job %s",
                self.job_id,
                exc_info=True,
            )

    async def record_terminal_outcome(
        self,
        *,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        """UPSERT terminal result/error payloads into ``job_progress``.

        Outcome persistence is best-effort for the same reason progress is:
        losing the UI payload is bad, but it must not turn a completed handler
        into a failed Procrastinate job.
        """
        if self._pool is None or not self.job_id:
            logger.debug(
                "ctx_shim.record_terminal_outcome no-op (pool=%s, job_id=%r)",
                self._pool,
                self.job_id,
            )
            return
        try:
            await self._pool.execute(
                """
                INSERT INTO job_progress (jarvis_job_id, result, error, updated_at)
                VALUES ($1, $2, $3, NOW())
                ON CONFLICT (jarvis_job_id) DO UPDATE
                  SET result     = EXCLUDED.result,
                      error      = EXCLUDED.error,
                      updated_at = EXCLUDED.updated_at
                """,
                self.job_id,
                result,
                error,
            )
        except Exception:  # noqa: BLE001 — never let outcome reporting kill the job
            logger.debug(
                "ctx_shim.record_terminal_outcome UPSERT failed for job %s",
                self.job_id,
                exc_info=True,
            )

    async def is_cancelled(self) -> bool:
        """Return True when procrastinate has flagged the job for abort.

        Bridges to ``procrastinate.JobContext.should_abort()`` (sync, so we
        wrap the call). When no procrastinate context is attached (unit
        tests instantiating the shim directly), always returns False.
        """
        if self._procrastinate_ctx is None:
            return False
        try:
            return bool(self._procrastinate_ctx.should_abort())
        except Exception:  # noqa: BLE001 — defensive: never raise from a cancellation probe
            logger.debug(
                "ctx_shim.is_cancelled probe failed for job %s",
                self.job_id,
                exc_info=True,
            )
            return False


def make_ctx_shim(
    procrastinate_ctx: _ProcrastinateJobContext | None = None,
    *,
    job_id: str | None = None,
    pool: asyncpg.Pool | None = None,
) -> ProcrastinateJobContextShim:
    """Build a ``ProcrastinateJobContextShim`` from a procrastinate ``JobContext``.

    Args:
        procrastinate_ctx: the ``procrastinate.JobContext`` instance handed to
            a ``@app.task(pass_context=True)`` body. May be ``None`` for tests
            that exercise the shim directly.
        job_id: explicit override for ``job_id``. If omitted, derived from
            ``procrastinate_ctx.job.task_kwargs['job_id']`` (the JARVIS UUID),
            falling back to ``str(procrastinate_ctx.job.id)`` (the
            procrastinate bigint id) and finally to ``""``.
        pool: asyncpg pool used by ``update_progress`` to UPSERT into
            ``job_progress``. When ``None``, progress reporting is a no-op
            (safe for unit tests).
    """
    if job_id is None:
        if procrastinate_ctx is not None:
            try:
                kwargs = procrastinate_ctx.job.task_kwargs or {}
                kwarg_id = kwargs.get("job_id")
                if kwarg_id:
                    job_id = str(kwarg_id)
                else:
                    job_id = str(procrastinate_ctx.job.id)
            except AttributeError:
                job_id = ""
        else:
            job_id = ""
    return ProcrastinateJobContextShim(
        job_id=job_id,
        procrastinate_ctx=procrastinate_ctx,
        pool=pool,
    )
