"""Task registration for the learning_engine service (W4-1 dependency inversion).

This module owns the kind→handler mapping for all JARVIS job kinds serviced
by learning_engine. Call ``register_learning_engine_tasks(app)`` during
lifespan startup *before* ``app.run_worker_async()`` is started.
"""

from __future__ import annotations

import procrastinate
from jarvis_common.task_registry import register_tasks

from learning_engine.routers.generation import _card_generate_batch_job, _card_generate_job

# ---------------------------------------------------------------------------
# kind → handler mapping for the learning_engine queue
# ---------------------------------------------------------------------------

KIND_TO_HANDLER: dict[str, object] = {
    "card.generate": _card_generate_job,
    "card.generate_batch": _card_generate_batch_job,
}


def register_learning_engine_tasks(procrastinate_app: procrastinate.App) -> None:
    """Register all learning_engine job handlers on ``procrastinate_app``.

    Must be called during lifespan startup before the worker is started.
    Raises AssertionError if any kind is missing from ``app.tasks`` after
    registration (startup-hook assertion per adversarial review).
    """
    register_tasks(procrastinate_app, mapping=KIND_TO_HANDLER, queue="learning_engine")  # type: ignore[arg-type]

    # Startup-hook assertion: every registered kind must appear in app.tasks.
    missing = [kind for kind in KIND_TO_HANDLER if kind not in procrastinate_app.tasks]
    assert not missing, f"register_learning_engine_tasks: failed to register kinds: {missing}"
