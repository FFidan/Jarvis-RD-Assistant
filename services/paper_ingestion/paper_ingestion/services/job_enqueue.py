"""Shared enqueue scaffolding for routes that answer with the job envelope.

Every route that hands long-running work to Procrastinate repeats the same three
steps: mint a JARVIS job id, defer the registered task with that id and the
calling user, then answer with the queued envelope. This module bundles the
three steps for the routes that call it, but it is not yet the single
definition: most deferring call sites still mint ``str(uuid.uuid4())`` and pass
``job_id=``/``user_id=`` to ``defer_async`` themselves, so a change to the id
format, the keyword names, or the envelope has to be carried to those call
sites as well.

Task lookup deliberately stays at the call site: routes differ in how they react
to an unregistered task, and one of them answers with its own error rather than
the registry's KeyError.
"""

import uuid
from typing import Any, Protocol

from jarvis_common import JobCreateResponse


class DeferrableTask(Protocol):
    """The single Procrastinate capability an enqueueing route needs.

    ``KIND_TO_TASK`` values are typed ``Any``, so this states structurally what
    a caller has to hand over instead of naming a task class whose generic
    parameters carry no useful information here.
    """

    async def defer_async(self, **task_kwargs: Any) -> int: ...


async def enqueue_job(
    task: DeferrableTask,
    *,
    user_id: int | None,
    **payload: Any,
) -> JobCreateResponse:
    """Defer *task* for *user_id* and return the queued job envelope.

    Parameters
    ----------
    task : DeferrableTask
        Registered Procrastinate task, already resolved by the caller.
    user_id : int | None
        Owner recorded on the job. ``None`` where the route admits a caller
        that carries no user identity.
    **payload : Any
        Task-specific keyword arguments, forwarded to ``defer_async`` unchanged.

    Returns
    -------
    JobCreateResponse
        ``status="queued"`` carrying the job id this call minted.

    """
    job_id = str(uuid.uuid4())
    await task.defer_async(job_id=job_id, user_id=user_id, **payload)
    return JobCreateResponse(job_id=job_id, status="queued")
