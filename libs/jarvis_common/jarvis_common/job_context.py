"""Execution-context adapter between procrastinate and JARVIS job handlers.

Job handlers receive a context satisfying the
``jarvis_common.jobs.ProgressContext`` protocol (``job_id: str``,
``async update_progress(progress, message=None)``, ``async is_cancelled()``,
``async record_terminal_outcome(...)``). Procrastinate hands tasks its own
``procrastinate.JobContext`` — a different object with ``context.job.id``,
``context.should_abort()``, etc.

``ProcrastinateJobContextShim`` is the concrete adapter between the two.
``task_registry`` builds one via ``make_ctx_shim`` for every handler it
dispatches; stalled-job reclamation in ``app_factory`` constructs the shim
directly to persist interrupted outcomes.

- ``update_progress``: UPSERTs into the ``job_progress`` table (migration
  054). Silently degrades to a no-op when no pool is supplied or the table
  is missing — older DBs without migration 054 still run, they just don't
  surface progress.
- ``record_terminal_outcome``: persists the job's terminal result or error
  payload into the same table, best-effort.
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
    """Adapter implementing the ``jarvis_common.jobs.ProgressContext`` protocol.

    The handler-facing contract (see ``ProgressContext`` in ``jobs.py``):
        - ``job_id: str``                         — attribute
        - ``async update_progress(progress, message=None) -> None``
        - ``async is_cancelled() -> bool``
        - ``async record_terminal_outcome(*, result=None, error=None, is_error=False) -> bool``
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
                "SELECT ops.record_job_progress_v1($1, $2, $3)",
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
        is_error: bool = False,
    ) -> bool:
        """UPSERT terminal result/error payloads into ``job_progress``.

        Outcome persistence is best-effort: losing the UI payload is bad, but
        it must not turn a completed handler into a failed Procrastinate job.
        Returns ``True`` when the outcome was persisted (or there was nothing
        to persist) and ``False`` when a write was attempted and failed, so a
        caller such as stalled-job reclamation can surface the loss.

        Progress semantics on terminal write:
        - **New row** (no prior ``update_progress`` call): ``progress`` is
          written as ``1.0`` on success or ``0.0`` on error — determined by
          the ``CASE WHEN $4`` in the ``INSERT VALUES``.
        - **Existing row** (a prior ``update_progress(p)`` wrote a non-NULL
          value): the ``ON CONFLICT UPDATE`` arm uses
          ``COALESCE(job_progress.progress, EXCLUDED.progress)`` to **preserve
          the in-flight value**.  The terminal call is **NOT** authoritative
          on ``progress`` for existing rows.

        Rationale: the last ``update_progress()`` call is the most recent
        user-visible signal.  Overwriting it with ``1.0``/``0.0`` on terminal
        would stutter the UI for long-running jobs.  Renderers that want
        "100% on success" should derive that from the ``is_error`` and
        ``result``/``error`` columns, not from ``progress``.
        """
        if self._pool is None or not self.job_id:
            logger.debug(
                "ctx_shim.record_terminal_outcome no-op (pool=%s, job_id=%r)",
                self._pool,
                self.job_id,
            )
            return True
        try:
            await self._pool.execute(
                "SELECT ops.record_job_outcome_v1($1, $2::jsonb, $3::jsonb, $4)",
                self.job_id,
                result,
                error,
                is_error,
            )
        except Exception:  # noqa: BLE001 — never let outcome reporting kill the job
            logger.debug(
                "ctx_shim.record_terminal_outcome UPSERT failed for job %s",
                self.job_id,
                exc_info=True,
            )
            return False
        return True

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

    Parameters
    ----------
    procrastinate_ctx:
        The ``procrastinate.JobContext`` instance handed to a
        ``@app.task(pass_context=True)`` body.  May be ``None`` for tests
        that exercise the shim directly.
    job_id:
        Explicit override for the JARVIS job UUID.  When omitted, derived from
        ``procrastinate_ctx.job.task_kwargs['job_id']`` (the JARVIS UUID),
        falling back to ``str(procrastinate_ctx.job.id)`` (the procrastinate
        bigint id), and finally to ``""``.
    pool:
        asyncpg pool used by :meth:`ProcrastinateJobContextShim.update_progress`
        to UPSERT into ``job_progress``.  When ``None``, progress reporting is
        a no-op (safe for unit tests).

    Returns
    -------
    ProcrastinateJobContextShim
        A shim with ``job_id`` resolved and the pool attached.

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
