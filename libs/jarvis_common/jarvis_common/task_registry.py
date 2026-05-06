"""Procrastinate task registry — all JARVIS job kinds.

Step 2 of the B.4 cutover (spec: ``docs/specs/2026-05-03-b4-job-broker.md``).
The procrastinate worker is not yet wired into any service lifespan and no
enqueue path uses these tasks — they exist purely to register with
procrastinate so they are discoverable by ``app.run_worker_async()`` once
B.2 lands. Step 2 is **purely additive**: the legacy ``worker_loop`` and all
existing ``@job_handler`` decorators are untouched.

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

Verified handler symbols matching the keys of ``JOB_HANDLER_OWNER``:

    paper.process              -> paper_ingestion.paper_jobs._paper_process_job
    paper.analyze              -> paper_ingestion.paper_jobs._paper_analyze_job
    paper.summarize            -> paper_ingestion.paper_jobs._paper_summarize_job
    papers.batch_process       -> paper_ingestion.paper_jobs._papers_batch_process_job
    papers.batch_summarize     -> paper_ingestion.paper_jobs._papers_batch_summarize_job
    papers.scan_local          -> paper_ingestion.paper_jobs._papers_scan_local_job
    citations.batch_fetch      -> paper_ingestion.citations_jobs._citations_batch_fetch_job
    digest.weekly              -> paper_ingestion.paper_jobs._digest_weekly_job
    extraction.single          -> paper_ingestion.extraction.jobs._extraction_single_job
    extraction.batch           -> paper_ingestion.extraction.jobs._extraction_batch_job
    contradictions.scan        -> paper_ingestion.contradiction_jobs._contradictions_scan_job
    pulse.generate             -> paper_ingestion.pulse.job._pulse_generate_job
    pulse.train_classifier     -> paper_ingestion.pulse.training._pulse_train_classifier_job
    model.pull                 -> paper_ingestion.services.model_lifecycle._model_pull_job
    zotero.push                -> paper_ingestion.integrations.zotero_service._zotero_push_job
    zotero.resync              -> paper_ingestion.integrations.zotero_service._zotero_resync_job
    zotero.sync_from_zotero    -> paper_ingestion.integrations.zotero_service
                                       ._zotero_sync_from_zotero_job
    zotero.sync_annotations    -> paper_ingestion.integrations.zotero_service
                                       ._zotero_sync_annotations_job
    card.generate              -> learning_engine.routers.generation._card_generate_job
    card.generate_batch        -> learning_engine.routers.generation._card_generate_batch_job

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
    happens at module import time.
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
    pool, http_client = _require_dependencies()
    ctx = make_ctx_shim(context, pool=pool)
    try:
        result = await handler(pool, http_client, payload, ctx)
    except Exception as exc:
        await ctx.record_terminal_outcome(error=_terminal_error_payload(exc))
        raise
    await ctx.record_terminal_outcome(result=result)
    return result


# ---------------------------------------------------------------------------
# Task definitions
# ---------------------------------------------------------------------------
#
# Each task:
#   - registers under the dotted kind name from JOB_HANDLER_OWNER (jobs.py:46)
#   - declares its queue (= the owning service from the same map)
#   - sets ``pass_context=True`` so the dispatcher can build a JobContext shim
#   - imports the legacy handler lazily inside the body so this module stays
#     importable from any service (lazy import avoids cross-service import
#     cycles + lets unit tests import task_registry without dragging in
#     paper_ingestion / learning_engine).


@app.task(name="paper.process", queue="paper_ingestion", pass_context=True)
async def paper_process(context: procrastinate.JobContext, **payload: Any) -> dict[str, Any]:
    from paper_ingestion.paper_jobs import _paper_process_job

    return await _run_legacy_handler(context, payload, _paper_process_job)


@app.task(name="paper.analyze", queue="paper_ingestion", pass_context=True)
async def paper_analyze(context: procrastinate.JobContext, **payload: Any) -> dict[str, Any]:
    from paper_ingestion.paper_jobs import _paper_analyze_job

    return await _run_legacy_handler(context, payload, _paper_analyze_job)


@app.task(name="paper.summarize", queue="paper_ingestion", pass_context=True)
async def paper_summarize(context: procrastinate.JobContext, **payload: Any) -> dict[str, Any]:
    from paper_ingestion.paper_jobs import _paper_summarize_job

    return await _run_legacy_handler(context, payload, _paper_summarize_job)


@app.task(name="papers.batch_process", queue="paper_ingestion", pass_context=True)
async def papers_batch_process(context: procrastinate.JobContext, **payload: Any) -> dict[str, Any]:
    from paper_ingestion.paper_jobs import _papers_batch_process_job

    return await _run_legacy_handler(context, payload, _papers_batch_process_job)


@app.task(name="papers.batch_summarize", queue="paper_ingestion", pass_context=True)
async def papers_batch_summarize(
    context: procrastinate.JobContext, **payload: Any
) -> dict[str, Any]:
    from paper_ingestion.paper_jobs import _papers_batch_summarize_job

    return await _run_legacy_handler(context, payload, _papers_batch_summarize_job)


@app.task(name="papers.scan_local", queue="paper_ingestion", pass_context=True)
async def papers_scan_local(context: procrastinate.JobContext, **payload: Any) -> dict[str, Any]:
    from paper_ingestion.paper_jobs import _papers_scan_local_job

    return await _run_legacy_handler(context, payload, _papers_scan_local_job)


@app.task(name="citations.batch_fetch", queue="paper_ingestion", pass_context=True)
async def citations_batch_fetch(
    context: procrastinate.JobContext, **payload: Any
) -> dict[str, Any]:
    from paper_ingestion.citations_jobs import _citations_batch_fetch_job

    return await _run_legacy_handler(context, payload, _citations_batch_fetch_job)


@app.task(name="digest.weekly", queue="paper_ingestion", pass_context=True)
async def digest_weekly(context: procrastinate.JobContext, **payload: Any) -> dict[str, Any]:
    from paper_ingestion.paper_jobs import _digest_weekly_job

    return await _run_legacy_handler(context, payload, _digest_weekly_job)


@app.task(name="extraction.single", queue="paper_ingestion", pass_context=True)
async def extraction_single(context: procrastinate.JobContext, **payload: Any) -> dict[str, Any]:
    from paper_ingestion.extraction.jobs import _extraction_single_job

    return await _run_legacy_handler(context, payload, _extraction_single_job)


@app.task(name="extraction.batch", queue="paper_ingestion", pass_context=True)
async def extraction_batch(context: procrastinate.JobContext, **payload: Any) -> dict[str, Any]:
    from paper_ingestion.extraction.jobs import _extraction_batch_job

    return await _run_legacy_handler(context, payload, _extraction_batch_job)


@app.task(name="contradictions.scan", queue="paper_ingestion", pass_context=True)
async def contradictions_scan(context: procrastinate.JobContext, **payload: Any) -> dict[str, Any]:
    from paper_ingestion.contradiction_jobs import _contradictions_scan_job

    return await _run_legacy_handler(context, payload, _contradictions_scan_job)


@app.task(name="pulse.generate", queue="paper_ingestion", pass_context=True)
async def pulse_generate(context: procrastinate.JobContext, **payload: Any) -> dict[str, Any]:
    from paper_ingestion.pulse.job import _pulse_generate_job

    return await _run_legacy_handler(context, payload, _pulse_generate_job)


@app.task(name="pulse.train_classifier", queue="paper_ingestion", pass_context=True)
async def pulse_train_classifier(
    context: procrastinate.JobContext, **payload: Any
) -> dict[str, Any]:
    from paper_ingestion.pulse.training import _pulse_train_classifier_job

    return await _run_legacy_handler(context, payload, _pulse_train_classifier_job)


@app.task(name="model.pull", queue="paper_ingestion", pass_context=True)
async def model_pull(context: procrastinate.JobContext, **payload: Any) -> dict[str, Any]:
    from paper_ingestion.services.model_lifecycle import _model_pull_job

    return await _run_legacy_handler(context, payload, _model_pull_job)


@app.task(name="zotero.push", queue="paper_ingestion", pass_context=True)
async def zotero_push(context: procrastinate.JobContext, **payload: Any) -> dict[str, Any]:
    from paper_ingestion.integrations.zotero_service import _zotero_push_job

    return await _run_legacy_handler(context, payload, _zotero_push_job)


@app.task(name="zotero.resync", queue="paper_ingestion", pass_context=True)
async def zotero_resync(context: procrastinate.JobContext, **payload: Any) -> dict[str, Any]:
    from paper_ingestion.integrations.zotero_service import _zotero_resync_job

    return await _run_legacy_handler(context, payload, _zotero_resync_job)


@app.task(name="zotero.sync_from_zotero", queue="paper_ingestion", pass_context=True)
async def zotero_sync_from_zotero(
    context: procrastinate.JobContext, **payload: Any
) -> dict[str, Any]:
    from paper_ingestion.integrations.zotero_service import _zotero_sync_from_zotero_job

    return await _run_legacy_handler(context, payload, _zotero_sync_from_zotero_job)


@app.task(name="zotero.sync_annotations", queue="paper_ingestion", pass_context=True)
async def zotero_sync_annotations(
    context: procrastinate.JobContext, **payload: Any
) -> dict[str, Any]:
    from paper_ingestion.integrations.zotero_service import _zotero_sync_annotations_job

    return await _run_legacy_handler(context, payload, _zotero_sync_annotations_job)


@app.task(name="card.generate", queue="learning_engine", pass_context=True)
async def card_generate(context: procrastinate.JobContext, **payload: Any) -> dict[str, Any]:
    from learning_engine.routers.generation import _card_generate_job

    return await _run_legacy_handler(context, payload, _card_generate_job)


@app.task(name="card.generate_batch", queue="learning_engine", pass_context=True)
async def card_generate_batch(context: procrastinate.JobContext, **payload: Any) -> dict[str, Any]:
    from learning_engine.routers.generation import _card_generate_batch_job

    return await _run_legacy_handler(context, payload, _card_generate_batch_job)


# ---------------------------------------------------------------------------
# KIND_TO_TASK mapping — used by create_job for procrastinate dispatch
# ---------------------------------------------------------------------------
#
# Maps each JARVIS job kind string to its registered procrastinate task object.
# The ``create_job`` route uses this to defer a task via procrastinate instead
# of inserting a legacy row, so GET/stream/cancel routes (now backed by
# ``get_unified``) can find the job in ``procrastinate_jobs``.
#
# ``noop.test`` is intentionally absent — the noop handler is a legacy-only
# test helper and falls through to the legacy ``enqueue`` path.

KIND_TO_TASK: dict[str, Any] = {
    "paper.process": paper_process,
    "paper.analyze": paper_analyze,
    "paper.summarize": paper_summarize,
    "papers.batch_process": papers_batch_process,
    "papers.batch_summarize": papers_batch_summarize,
    "papers.scan_local": papers_scan_local,
    "citations.batch_fetch": citations_batch_fetch,
    "digest.weekly": digest_weekly,
    "extraction.single": extraction_single,
    "extraction.batch": extraction_batch,
    "contradictions.scan": contradictions_scan,
    "pulse.generate": pulse_generate,
    "pulse.train_classifier": pulse_train_classifier,
    "model.pull": model_pull,
    "zotero.push": zotero_push,
    "zotero.resync": zotero_resync,
    "zotero.sync_from_zotero": zotero_sync_from_zotero,
    "zotero.sync_annotations": zotero_sync_annotations,
    "card.generate": card_generate,
    "card.generate_batch": card_generate_batch,
}

# ---------------------------------------------------------------------------
# Public exports
# ---------------------------------------------------------------------------

__all__ = [
    "app",
    "set_dependencies",
    "KIND_TO_TASK",
]
