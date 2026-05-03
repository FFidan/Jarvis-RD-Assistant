"""JobContext-compatible shim bridging procrastinate ↔ legacy ``jarvis_common.jobs.JobContext``.

Step 2 of the B.4 cutover (spec: docs/specs/2026-05-03-b4-job-broker.md).

The legacy 19 ``@job_handler``-decorated functions all expect a
``jarvis_common.jobs.JobContext`` (a ``@dataclass`` with ``job_id: str``,
``async update_progress(progress, message=None)``, ``async is_cancelled()``).
Procrastinate hands tasks its own ``procrastinate.JobContext`` — a different
object with ``context.job.id``, ``context.should_abort()``, etc.

This module provides a thin adapter so the legacy handlers can be invoked
unchanged from inside a procrastinate task body. The adapter is intentionally
no-op-shaped for Step 2:

- ``update_progress``: logs a debug line; persistence will be wired in Step 3
  (SSE bridge / progress storage).
- ``is_cancelled``: returns ``False`` for now; Step 3 will route this through
  procrastinate's ``should_abort()`` once the cancel-on-defer story is decided.
- ``job_id``: extracted from the procrastinate context's ``job.id`` (an
  integer) and stringified to match the legacy ``str`` type.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    # Imported lazily — procrastinate is in the dependency graph but importing
    # it eagerly would couple every consumer of jarvis_common to libpq being
    # available, which is undesirable for unit tests.
    from procrastinate import JobContext as _ProcrastinateJobContext


class ProcrastinateJobContextShim:
    """Adapter exposing the legacy ``jarvis_common.jobs.JobContext`` surface.

    The legacy contract (see ``libs/jarvis_common/jarvis_common/jobs.py:324``):
        - ``job_id: str``                         — attribute
        - ``async update_progress(progress, message=None) -> None``
        - ``async is_cancelled() -> bool``

    Step 2 only registers tasks; no live enqueue path uses them yet, so the
    methods are deliberately minimal. Step 3 will swap the bodies for real
    persistence once the SSE bridge is in place.
    """

    __slots__ = ("job_id", "_procrastinate_ctx")

    def __init__(
        self,
        *,
        job_id: str,
        procrastinate_ctx: Any | None = None,
    ) -> None:
        self.job_id = job_id
        self._procrastinate_ctx = procrastinate_ctx

    async def update_progress(
        self,
        progress: float,
        message: str | None = None,
    ) -> None:
        """Step 2 no-op: log progress at debug level.

        Step 3 will route this to the same DB row + ``notify_job_update`` path
        that the legacy ``JobContext.update_progress`` uses today.
        """
        logger.debug(
            "task_registry shim: job=%s progress=%.2f msg=%s (no-op until Step 3)",
            self.job_id,
            progress,
            message,
        )

    async def is_cancelled(self) -> bool:
        """Step 2 stub: always False.

        Step 3 will bridge to ``procrastinate_ctx.should_abort()`` once the
        cancel-on-defer flow is wired up.
        """
        return False


def make_ctx_shim(
    procrastinate_ctx: _ProcrastinateJobContext | None = None,
    *,
    job_id: str | None = None,
) -> ProcrastinateJobContextShim:
    """Build a ``ProcrastinateJobContextShim`` from a procrastinate ``JobContext``.

    Args:
        procrastinate_ctx: the ``procrastinate.JobContext`` instance handed to
            a ``@app.task(pass_context=True)`` body. May be ``None`` for tests
            that exercise the shim directly.
        job_id: explicit override for ``job_id``. If omitted, derived from
            ``procrastinate_ctx.job.id`` (stringified). Falls back to ``""``
            when neither is available.
    """
    if job_id is None:
        if procrastinate_ctx is not None:
            try:
                job_id = str(procrastinate_ctx.job.id)
            except AttributeError:
                job_id = ""
        else:
            job_id = ""
    return ProcrastinateJobContextShim(
        job_id=job_id,
        procrastinate_ctx=procrastinate_ctx,
    )
