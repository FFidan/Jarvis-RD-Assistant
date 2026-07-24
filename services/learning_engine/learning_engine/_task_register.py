"""Task registration for the learning_engine service (dependency inversion).

This module owns the kind→handler mapping for all JARVIS job kinds serviced
by learning_engine. Call ``register_learning_engine_tasks(app)`` during
lifespan startup *before* ``app.run_worker_async()`` is started.
"""

from __future__ import annotations

import procrastinate
from jarvis_common.task_registry import register_service_tasks

from learning_engine.generation_service import _card_generate_batch_job, _card_generate_job

# ---------------------------------------------------------------------------
# kind → handler mapping for the learning_engine queue
# ---------------------------------------------------------------------------

KIND_TO_HANDLER: dict[str, object] = {
    "card.generate": _card_generate_job,
    "card.generate_batch": _card_generate_batch_job,
}


def register_learning_engine_tasks(procrastinate_app: procrastinate.App) -> None:
    """Register all learning_engine job handlers on ``procrastinate_app``.

    Must be called during lifespan startup before the worker is started. The
    consume-queue is derived from ``JOB_HANDLER_OWNER`` (via ``queue_for_kind``)
    so a queue rename in the owner map propagates here automatically. Raises
    ``RuntimeError`` if the handled kinds span multiple owner queues, or if any
    kind is missing from ``app.tasks`` after registration (fail-fast at startup,
    -O-proof).
    """
    register_service_tasks(
        procrastinate_app,
        KIND_TO_HANDLER,  # type: ignore[arg-type]
        service_label="register_learning_engine_tasks",
    )
