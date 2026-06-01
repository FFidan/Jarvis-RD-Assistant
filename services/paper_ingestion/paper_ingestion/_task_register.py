"""Task registration for the paper_ingestion service (dependency inversion).

This module owns the kind→handler mapping for all JARVIS job kinds serviced
by paper_ingestion. Call ``register_paper_ingestion_tasks(app)`` during
lifespan startup *before* ``app.run_worker_async()`` is started.

Handler imports are at module level (not lazy) because this module is only
imported by paper_ingestion's lifespan, so there is no cross-service cycle risk.
"""

from __future__ import annotations

import procrastinate
from jarvis_common.settings import get_jobs_settings
from jarvis_common.task_registry import register_tasks

from paper_ingestion.citations_jobs import _citations_batch_fetch_job
from paper_ingestion.contradiction_jobs import _contradictions_scan_job
from paper_ingestion.extraction.jobs import _extraction_batch_job, _extraction_single_job
from paper_ingestion.integrations.zotero_service import (
    _zotero_push_job,
    _zotero_resync_job,
    _zotero_sync_annotations_job,
    _zotero_sync_from_zotero_job,
)
from paper_ingestion.paper_jobs import (
    _digest_weekly_job,
    _paper_analyze_job,
    _paper_process_job,
    _paper_summarize_job,
    _papers_batch_process_job,
    _papers_batch_summarize_job,
    _papers_scan_local_job,
)
from paper_ingestion.pulse.job import _pulse_generate_job
from paper_ingestion.pulse.training import _pulse_train_classifier_job
from paper_ingestion.services.model_lifecycle import _model_pull_job

# ---------------------------------------------------------------------------
# kind → handler mapping for the paper_ingestion queue
# ---------------------------------------------------------------------------

KIND_TO_HANDLER: dict[str, object] = {
    "paper.process": _paper_process_job,
    "paper.analyze": _paper_analyze_job,
    "paper.summarize": _paper_summarize_job,
    "papers.batch_process": _papers_batch_process_job,
    "papers.batch_summarize": _papers_batch_summarize_job,
    "papers.scan_local": _papers_scan_local_job,
    "citations.batch_fetch": _citations_batch_fetch_job,
    "digest.weekly": _digest_weekly_job,
    "extraction.single": _extraction_single_job,
    "extraction.batch": _extraction_batch_job,
    "contradictions.scan": _contradictions_scan_job,
    "pulse.generate": _pulse_generate_job,
    "pulse.train_classifier": _pulse_train_classifier_job,
    "model.pull": _model_pull_job,
    "zotero.push": _zotero_push_job,
    "zotero.resync": _zotero_resync_job,
    "zotero.sync_from_zotero": _zotero_sync_from_zotero_job,
    "zotero.sync_annotations": _zotero_sync_annotations_job,
}


def register_paper_ingestion_tasks(procrastinate_app: procrastinate.App) -> None:
    """Register all paper_ingestion job handlers on ``procrastinate_app``.

    Must be called during lifespan startup before the worker is started.
    Raises AssertionError if any kind is missing from ``app.tasks`` after
    registration (startup-hook assertion per adversarial review).
    """
    register_tasks(procrastinate_app, mapping=KIND_TO_HANDLER, queue="paper_ingestion")  # type: ignore[arg-type]

    # Startup-hook assertion: every registered kind must appear in app.tasks.
    missing = [kind for kind in KIND_TO_HANDLER if kind not in procrastinate_app.tasks]
    assert not missing, f"register_paper_ingestion_tasks: failed to register kinds: {missing}"

    # noop.test is registered directly on the app in task_registry (always);
    # wire it into the internal task map when test jobs are enabled.
    if get_jobs_settings().test_jobs_enabled:
        from jarvis_common.task_registry import _TASK_MAP, noop_task  # noqa: PLC0415

        if "noop.test" not in _TASK_MAP:
            _TASK_MAP["noop.test"] = noop_task
