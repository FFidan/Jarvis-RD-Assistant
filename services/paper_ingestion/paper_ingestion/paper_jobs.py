"""Job handlers for paper ingestion ops: paper.process and paper.analyze.

These handlers are called by procrastinate task wrappers in task_registry.py.

Handler signature: async (pool, http_client, payload, ctx) -> dict
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Any, cast

import asyncpg
import httpx
from jarvis_common.db_helpers import assert_paper_ownership, assert_papers_ownership
from jarvis_common.jobs import JobError, ProgressContext, batch_terminal_status
from jarvis_common.library import is_in_library

from paper_ingestion._state import get_services
from paper_ingestion.job_errors import classify_bulk_error
from paper_ingestion.pdf_processor import PDF_STORAGE_PATH, resolve_safe_pdf_path

if TYPE_CHECKING:
    from collections.abc import Coroutine

    from paper_ingestion.pdf_processor import PDFProcessor
    from paper_ingestion.services.pdf_workflow import EmbeddingReconcileResult, ProcessPdfResult
    from paper_ingestion.services.summarization import SummaryGenerationResult

logger = logging.getLogger(__name__)

__all__ = [
    "_SubCtx",
    "PDF_STORAGE_PATH",
    "_paper_process_job",
    "_paper_analyze_job",
    "_paper_summarize_job",
    "_papers_batch_process_job",
    "_papers_batch_summarize_job",
    "_papers_process_library_job",
    "_papers_scan_local_job",
    "_digest_weekly_job",
]


# ---------------------------------------------------------------------------
# _SubCtx — progress scaling helper
# ---------------------------------------------------------------------------


class _SubCtx:
    """Wraps a ProgressContext and scales inner progress (0–1) to an outer range.

    Example: ``_SubCtx(ctx, 0.2, 0.7)`` maps inner 0 → 0.2 and inner 1 → 0.7.
    """

    def __init__(self, ctx: ProgressContext, start: float, end: float) -> None:
        self._ctx = ctx
        self._start = start
        self._end = end

    async def update_progress(self, progress: float, message: str | None = None) -> None:
        """Report scaled progress to the outer job context.

        Parameters
        ----------
        progress : float
            Inner progress (0.0–1.0); mapped linearly to [start, end].
        message : str | None
            Optional human-readable status string forwarded to the outer context.
        """
        scaled = self._start + progress * (self._end - self._start)
        await self._ctx.update_progress(scaled, message)

    async def is_cancelled(self) -> bool:
        """Return ``True`` if the outer job context has been cancelled."""
        return await self._ctx.is_cancelled()


# ---------------------------------------------------------------------------
# paper.process handler
# ---------------------------------------------------------------------------


async def _paper_process_job(
    pool: asyncpg.Pool,
    http_client: httpx.AsyncClient,
    payload: dict[str, Any],
    ctx: ProgressContext,
) -> dict[str, Any]:
    """Process a downloaded paper's PDF: extract, chunk, embed.

    Payload keys:
        paper_id (int): DB paper ID — PDF must already be downloaded.
        force (bool): re-process even if chunks already exist.

    A ``force`` run discards the paper's existing derived content, so it
    requires the paper to be in the caller's library. Read visibility is not
    enough: ``assert_paper_ownership`` also admits any public paper. The check
    here turns a doomed job into an immediate refusal; ``run_process_pdf`` holds
    the same rule for every ``force`` rebuild, whichever path reaches it.
    """
    from paper_ingestion.services.pdf_workflow import (
        PDFRebuildNotPermittedError,
        PDFUserFacingError,
        run_process_pdf,
    )

    paper_id: int = payload["paper_id"]
    user_id: int | None = payload.get("user_id")
    force: bool = bool(payload.get("force", False))
    async with pool.acquire() as conn:
        await assert_paper_ownership(conn, paper_id, user_id)
        # A fast-fail for a requester this payload names, before the row read and
        # the service lookups below. An unnamed one is not admitted by skipping
        # it: ``run_process_pdf`` requires a holding requester for a ``force``
        # rebuild and refuses ``None`` outright.
        if force and user_id is not None:
            if not await is_in_library(conn, user_id=user_id, paper_id=paper_id):
                raise JobError(
                    f"Paper {paper_id} must be in your library before its content can be rebuilt"
                )

    # Load paper row to get pdf_local_path
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, pdf_downloaded, pdf_local_path FROM papers WHERE id = $1",
            paper_id,
        )

    if not row:
        raise JobError(f"Paper {paper_id} not found")
    if not row["pdf_downloaded"] or not row["pdf_local_path"]:
        raise JobError(f"PDF not yet downloaded for paper {paper_id}")

    pdf_path = resolve_safe_pdf_path(row["pdf_local_path"], PDF_STORAGE_PATH)
    if pdf_path is None:
        raise JobError(f"Invalid or missing PDF for paper {paper_id}")

    await ctx.update_progress(0.1, "Downloaded")

    services = get_services()
    pdf_processor = services.pdf_processor
    if pdf_processor is None:
        raise RuntimeError("pdf_processor not initialized")
    embedder = services.embedder
    if embedder is None:
        raise RuntimeError("embedder not initialized")

    try:
        result = await run_process_pdf(
            paper_id,
            pdf_path,
            pool,
            pdf_processor,
            embedder,
            force=force,
            ctx=_SubCtx(ctx, 0.1, 1.0),
            requester_id=user_id,
        )
    except PDFRebuildNotPermittedError as exc:
        raise JobError(str(exc)) from exc
    except PDFUserFacingError as exc:
        raise JobError(str(exc)) from exc
    return cast(dict[str, Any], result)


# ---------------------------------------------------------------------------
# paper.analyze handler (composite: download → process → summarize)
# ---------------------------------------------------------------------------


async def _analyze_download_stage(
    pool: asyncpg.Pool,
    pdf_processor: PDFProcessor,
    row: Any,
    ctx: ProgressContext,
    *,
    is_local: bool,
) -> Any:
    """Download the paper's PDF when one is still missing; return the current row.

    Local papers and already-downloaded ones report the skip and keep their row.
    """
    from paper_ingestion.services.pdf_workflow import (
        PDFRecordMissingError,
        download_and_store_pdf,
    )

    if is_local or row["pdf_downloaded"]:
        await ctx.update_progress(0.0, "Download skipped" if is_local else "Already downloaded")
        return row

    await ctx.update_progress(0.0, "Downloading PDF")
    paper_id = row["id"]
    try:
        return await download_and_store_pdf(pool, pdf_processor, row["pdf_url"], paper_id)
    except PDFRecordMissingError as exc:
        raise JobError(f"Paper {paper_id} changed while its PDF was downloading") from exc


async def _summarize_single_paper(
    paper_id: int,
    pool: asyncpg.Pool,
    http_client: httpx.AsyncClient,
    *,
    user_id: int | None,
    force: bool,
) -> Any:
    """Summarize one paper, keeping a requester-facing failure readable.

    Single-paper jobs report one outcome, so a message written for the requester
    becomes the job's own error. Batch runs deliberately do not use this: there
    the per-paper error string is what the caller sees, and one paper's failure
    must not discard the rest of the run.

    Callers validate the services first; this reads them back from the same
    accessor rather than threading two more collaborators through.
    """
    from paper_ingestion.services.pdf_workflow import PDFUserFacingError
    from paper_ingestion.services.summarization import generate_paper_summary

    services = get_services()
    try:
        return await generate_paper_summary(
            paper_id,
            pool,
            http_client,
            services.verifier,
            services.embedder,
            user_id=user_id,
            force=force,
        )
    except PDFUserFacingError as exc:
        raise JobError(str(exc)) from exc


async def _paper_analyze_job(
    pool: asyncpg.Pool,
    http_client: httpx.AsyncClient,
    payload: dict[str, Any],
    ctx: ProgressContext,
) -> dict[str, Any]:
    """Chain download → process → summarize for a single paper.

    Payload keys:
        paper_id (int): DB paper ID.
        force (bool): rebuild derived content instead of resuming it. Reported
            as a job failure unless the paper is in the caller's library.

    B6 fix: local papers (source_type='local' or pdf_local_path IS NOT NULL)
    skip the download step.
    """
    from paper_ingestion.services.pdf_workflow import (
        PDFRebuildNotPermittedError,
        PDFUserFacingError,
        run_process_pdf,
    )

    paper_id: int = payload["paper_id"]
    user_id: int | None = payload.get("user_id")
    force: bool = bool(payload.get("force", False))
    async with pool.acquire() as conn:
        await assert_paper_ownership(conn, paper_id, user_id)

    # Load paper row once
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, source_type, pdf_url, pdf_downloaded, pdf_local_path "
            "FROM papers WHERE id = $1",
            paper_id,
        )

    if not row:
        raise JobError(f"Paper {paper_id} not found")

    is_local = row["source_type"] == "local" or row["pdf_local_path"] is not None

    if not is_local and not row["pdf_url"]:
        raise JobError(f"Paper {paper_id} has no PDF URL")

    services = get_services()
    pdf_processor = services.pdf_processor
    if pdf_processor is None:
        raise RuntimeError("pdf_processor not initialized")
    embedder = services.embedder
    if embedder is None:
        raise RuntimeError("embedder not initialized")
    verifier = services.verifier
    if verifier is None:
        raise RuntimeError("verifier not initialized")

    # ---- Step 1: Download (skip for local) ----
    row = await _analyze_download_stage(pool, pdf_processor, row, ctx, is_local=is_local)

    # ---- Step 2: Process PDF ----
    await ctx.update_progress(0.2, "Processing PDF")

    pdf_path = resolve_safe_pdf_path(row["pdf_local_path"], PDF_STORAGE_PATH)
    if pdf_path is None:
        raise JobError(f"Invalid or missing PDF for paper {paper_id}")

    sub_ctx = _SubCtx(ctx, 0.2, 0.7)
    try:
        result = await run_process_pdf(
            paper_id,
            pdf_path,
            pool,
            pdf_processor,
            embedder,
            force=force,
            ctx=sub_ctx,
            requester_id=user_id,
        )
    except PDFRebuildNotPermittedError as exc:
        raise JobError(str(exc)) from exc
    except PDFUserFacingError as exc:
        raise JobError(str(exc)) from exc

    # ---- Step 3: Summarize ----
    await ctx.update_progress(0.7, "Summarizing")
    await _summarize_single_paper(paper_id, pool, http_client, user_id=user_id, force=force)

    await ctx.update_progress(1.0, "Done")
    composite: dict[str, Any] = {
        "paper_id": paper_id,
        "chunk_count": result.get("chunk_count", 0),
        "process_status": result.get("status"),
    }
    # Thread best-effort process-step warnings (e.g. Qdrant stale-vector cleanup
    # failure) into the composite result; omit the key entirely when clean.
    process_warnings = result.get("warnings")
    if process_warnings:
        composite["warnings"] = process_warnings
    return composite


async def _paper_summarize_job(
    pool: asyncpg.Pool,
    http_client: httpx.AsyncClient,
    payload: dict[str, Any],
    ctx: ProgressContext,
) -> dict[str, Any]:
    """Generate a quote-verified summary for a single paper."""
    paper_id: int = int(payload["paper_id"])
    user_id: int | None = payload.get("user_id")
    force = bool(payload.get("force", False))
    async with pool.acquire() as conn:
        await assert_paper_ownership(conn, paper_id, user_id)

    services = get_services()
    verifier = services.verifier
    if verifier is None:
        raise RuntimeError("verifier not initialized")
    embedder = services.embedder
    if embedder is None:
        raise RuntimeError("embedder not initialized")

    await ctx.update_progress(0.1, "Summarizing")
    result: SummaryGenerationResult = await _summarize_single_paper(
        paper_id, pool, http_client, user_id=user_id, force=force
    )
    await ctx.update_progress(1.0, "Done")
    job_result: dict[str, Any] = {
        "paper_id": paper_id,
        "summary_id": result.summary.id,
        "status": "summarized",
    }
    if result.coverage < 1.0:
        job_result["coverage"] = result.coverage
    if result.passes > 1:
        job_result["passes"] = result.passes
    return job_result


# ---------------------------------------------------------------------------
# papers.batch_process handler
# ---------------------------------------------------------------------------


async def _papers_batch_process_job(
    pool: asyncpg.Pool,
    http_client: httpx.AsyncClient,
    payload: dict[str, Any],
    ctx: ProgressContext,
) -> dict[str, Any]:
    """Process many papers' PDFs in a single background job.

    Payload keys:
        paper_ids (list[int]): DB paper IDs whose PDFs should be processed.
        force (bool): rebuild derived content instead of resuming it. Recorded
            as a per-paper error for any paper outside the caller's library.
    """
    from paper_ingestion.services.pdf_workflow import run_process_pdf

    paper_ids: list[int] = list(payload.get("paper_ids", []))
    user_id: int | None = payload.get("user_id")
    force: bool = bool(payload.get("force", False))
    total = len(paper_ids)
    if user_id is not None:
        async with pool.acquire() as conn:
            await assert_papers_ownership(conn, paper_ids, user_id)

    services = get_services()
    pdf_processor = services.pdf_processor
    if pdf_processor is None:
        raise RuntimeError("pdf_processor not initialized")
    embedder = services.embedder
    if embedder is None:
        raise RuntimeError("embedder not initialized")

    processed = 0
    skipped = 0
    errors: list[str] = []

    await ctx.update_progress(0.05, f"Starting: {total} papers")

    cancelled = False

    for i, paper_id in enumerate(paper_ids):
        if await ctx.is_cancelled():
            cancelled = True
            break
        inner_start = (i / max(total, 1)) * 0.9 + 0.05
        inner_end = ((i + 1) / max(total, 1)) * 0.9 + 0.05
        await ctx.update_progress(inner_start, f"Processing paper {paper_id} ({i + 1}/{total})")
        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT pdf_downloaded, pdf_local_path FROM papers WHERE id = $1",
                    paper_id,
                )
            if not row or not row["pdf_downloaded"] or not row["pdf_local_path"]:
                skipped += 1
                continue
            pdf_path = resolve_safe_pdf_path(row["pdf_local_path"], PDF_STORAGE_PATH)
            if pdf_path is None:
                skipped += 1
                continue
            sub_ctx = _SubCtx(ctx, inner_start, inner_end)
            await run_process_pdf(
                paper_id,
                pdf_path,
                pool,
                pdf_processor,
                embedder,
                force=force,
                ctx=sub_ctx,
                requester_id=user_id,
            )
            processed += 1
        except Exception as exc:  # noqa: BLE001
            logger.exception("Batch process failed for paper %s", paper_id)
            errors.append(f"Paper {paper_id}: {classify_bulk_error(exc)}")

    failed = len(errors)
    remaining = max(total - processed - skipped - failed, 0)
    status = batch_terminal_status(
        cancelled=cancelled,
        incomplete=bool(skipped or failed or remaining),
    )
    headline = "Done" if status == "ok" else status.title()
    await ctx.update_progress(
        1.0,
        f"{headline}: {processed} processed, {skipped} skipped, {failed} failed",
    )
    return {
        "processed": processed,
        "skipped": skipped,
        "errors": errors,
        "failed": failed,
        "total": total,
        "remaining": remaining,
        "status": status,
    }


# ---------------------------------------------------------------------------
# papers.process_library handler — whole-library per-paper stage machine
# ---------------------------------------------------------------------------


# Selection: one stable ID-keyset page from the caller's user_library. Every
# completed paper remains eligible for a cheap vector-identity probe because
# PostgreSQL cannot prove its deterministic Qdrant points still match its chunks.
# Shared with the process-library endpoint's emptiness pre-check. ``$1`` = user_id,
# ``$2`` = summarize, ``$3`` = page size, ``$4`` = last examined paper ID.
# paper_summaries is per-user (UNIQUE NULLS NOT DISTINCT
# (paper_id, user_id)); scope the LEFT JOIN with IS NOT DISTINCT FROM to match
# the batch-summarize selection (rag.py).
_PROCESS_LIBRARY_PAGE_SIZE = 100
_PROCESS_LIBRARY_COUNT = "SELECT COUNT(*) FROM user_library WHERE user_id = $1"
_PROCESS_LIBRARY_SELECTION = """
    SELECT p.id, p.source_type, p.pdf_url, p.pdf_downloaded, p.pdf_local_path,
           (p.chunked_at IS NULL OR NOT EXISTS (
               SELECT 1 FROM paper_chunks c WHERE c.paper_id = p.id
           )) AS needs_process,
           (p.chunked_at IS NOT NULL AND EXISTS (
               SELECT 1 FROM paper_chunks c WHERE c.paper_id = p.id
           )) AS needs_reconcile,
           ($2 AND s.paper_id IS NULL) AS needs_summary
    FROM papers p
    JOIN user_library ul ON ul.paper_id = p.id AND ul.user_id = $1
    LEFT JOIN paper_summaries s
      ON s.paper_id = p.id AND s.user_id IS NOT DISTINCT FROM $1
     AND s.content_generation = p.content_generation
    WHERE p.id > $4
    ORDER BY p.id
    LIMIT $3
"""


@dataclass(frozen=True)
class _LibraryRun:
    """Collaborators one process-library run shares across every selected paper.

    ``summarize_paper`` carries the caller's ``user_id`` bound at resolve time, so
    no stage can generate a summary outside the requesting tenant.
    """

    pool: asyncpg.Pool
    summarize: bool
    pdf_processor: PDFProcessor
    process_pdf: partial[Coroutine[Any, Any, ProcessPdfResult]]
    reconcile_embeddings: partial[Coroutine[Any, Any, EmbeddingReconcileResult]]
    summarize_paper: partial[Coroutine[Any, Any, SummaryGenerationResult]]


@dataclass(frozen=True)
class _PaperOutcome:
    """One paper's contribution to the process-library result."""

    downloaded: int = 0
    processed: int = 0
    summarized: int = 0
    blocked: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


@dataclass
class _LibraryProgress:
    """Mutable counters and per-paper details for one process-library run."""

    examined: int = 0
    downloaded: int = 0
    processed: int = 0
    summarized: int = 0
    blocked: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)

    def record(self, outcome: _PaperOutcome) -> None:
        """Add one completed paper outcome to the aggregate result."""
        self.examined += 1
        self.downloaded += outcome.downloaded
        self.processed += outcome.processed
        self.summarized += outcome.summarized
        if outcome.blocked is not None:
            self.blocked.append(outcome.blocked)
        if outcome.error is not None:
            self.errors.append(outcome.error)

    def result(self, total: int, *, cancelled: bool) -> dict[str, Any]:
        """Build the public job result with an honest terminal status."""
        remaining = max(total - self.examined, 0)
        status = batch_terminal_status(
            cancelled=cancelled,
            incomplete=bool(self.errors or self.blocked or remaining),
        )
        return {
            "status": status,
            "total": total,
            "examined": self.examined,
            "remaining": remaining,
            "downloaded": self.downloaded,
            "processed": self.processed,
            "summarized": self.summarized,
            "blocked": self.blocked,
            "errors": self.errors,
        }


def _resolve_library_run(
    pool: asyncpg.Pool,
    http_client: httpx.AsyncClient,
    user_id: int,
    summarize: bool,
) -> _LibraryRun:
    """Resolve the services and bind the workflow calls the stage machine needs."""
    from paper_ingestion.services.pdf_workflow import reconcile_paper_embeddings, run_process_pdf
    from paper_ingestion.services.summarization import generate_paper_summary

    services = get_services()
    pdf_processor = services.pdf_processor
    if pdf_processor is None:
        raise RuntimeError("pdf_processor not initialized")
    embedder = services.embedder
    if embedder is None:
        raise RuntimeError("embedder not initialized")
    verifier = services.verifier
    if verifier is None:
        raise RuntimeError("verifier not initialized")
    return _LibraryRun(
        pool=pool,
        summarize=summarize,
        pdf_processor=pdf_processor,
        process_pdf=partial(
            run_process_pdf,
            db_pool=pool,
            pdf_processor=pdf_processor,
            embedder=embedder,
        ),
        reconcile_embeddings=partial(
            reconcile_paper_embeddings,
            db_pool=pool,
            embedder=embedder,
        ),
        summarize_paper=partial(
            generate_paper_summary,
            db_pool=pool,
            http_client=http_client,
            verifier=verifier,
            embedder=embedder,
            user_id=user_id,
        ),
    )


async def _library_download_stage(row: Any, run: _LibraryRun) -> Any:
    """Download a paper's PDF, persist its local path and return that path."""
    from paper_ingestion.services.pdf_workflow import (
        PDFRecordMissingError,
        download_and_store_pdf,
    )

    paper_id = row["id"]
    try:
        updated = await download_and_store_pdf(
            run.pool,
            run.pdf_processor,
            row["pdf_url"],
            paper_id,
        )
    except PDFRecordMissingError as exc:
        raise JobError(f"Paper {paper_id} changed while its PDF was downloading") from exc
    return updated["pdf_local_path"]


async def _library_process_stage(
    paper_id: int,
    pdf_local_path: Any,
    run: _LibraryRun,
    sub_ctx: _SubCtx,
) -> None:
    """Extract, chunk and embed one paper's already-downloaded PDF."""
    pdf_path = resolve_safe_pdf_path(pdf_local_path, PDF_STORAGE_PATH)
    if pdf_path is None:
        raise JobError(f"Invalid or missing PDF for paper {paper_id}")
    await run.process_pdf(paper_id, pdf_path, ctx=sub_ctx)


async def _library_reconcile_stage(paper_id: int, run: _LibraryRun) -> bool:
    """Probe persisted vectors and return whether repair work was performed."""
    result = await run.reconcile_embeddings(paper_id)
    if result["status"] == "empty":
        raise JobError(f"Paper {paper_id} lost its persisted chunks during reconciliation")
    return result["status"] == "repaired"


def _library_pdf_plan(row: Any) -> tuple[bool, dict[str, Any] | None]:
    """Decide whether a paper needs its PDF downloaded and whether that is possible.

    Returns ``(should_download, blocked)``. An unreachable PDF blocks only a
    paper that still needs processing; one selected solely for its missing
    summary needs nothing from the PDF, so nothing about it is blocked.
    """
    is_local = row["source_type"] == "local" or row["pdf_local_path"] is not None
    needs_download = not is_local and not row["pdf_downloaded"]
    if needs_download and not row["pdf_url"]:
        if row["needs_process"]:
            return False, {"paper_id": row["id"], "reason": "no_pdf_source"}
        return False, None
    return needs_download, None


async def _run_library_paper_stages(row: Any, run: _LibraryRun, sub_ctx: _SubCtx) -> _PaperOutcome:
    """Run download → process → summarize for one paper, isolating its failures.

    A paper with no PDF source is blocked only for the stages that need the PDF:
    one already chunked still receives the summary it was selected for, and is
    reported as neither blocked nor failed because nothing it needed was skipped.
    """
    paper_id = row["id"]
    should_download, blocked = _library_pdf_plan(row)
    pdf_local_path = row["pdf_local_path"]
    chunked = not row["needs_process"]
    downloaded = 0
    processed = 0
    summarized = 0
    stage = "download"
    try:
        if should_download:
            pdf_local_path = await _library_download_stage(row, run)
            downloaded = 1

        if row["needs_process"] and blocked is None:
            stage = "process"
            await _library_process_stage(paper_id, pdf_local_path, run, sub_ctx)
            processed = 1
            chunked = True
        elif row["needs_reconcile"]:
            stage = "reconcile"
            processed = int(await _library_reconcile_stage(paper_id, run))

        if run.summarize and row["needs_summary"] and chunked:
            stage = "summarize"
            await run.summarize_paper(paper_id)
            summarized = 1
    except Exception as exc:  # noqa: BLE001
        logger.exception("Library processing failed for paper %s (stage=%s)", paper_id, stage)
        return _PaperOutcome(
            downloaded=downloaded,
            processed=processed,
            summarized=summarized,
            error={"paper_id": paper_id, "stage": stage, "error": classify_bulk_error(exc)},
        )
    return _PaperOutcome(
        downloaded=downloaded, processed=processed, summarized=summarized, blocked=blocked
    )


async def _fetch_library_page(
    pool: asyncpg.Pool,
    user_id: int,
    summarize: bool,
    cursor: int,
) -> list[Any]:
    """Fetch one keyset page and reaffirm that every row belongs to the caller."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            _PROCESS_LIBRARY_SELECTION,
            user_id,
            summarize,
            _PROCESS_LIBRARY_PAGE_SIZE,
            cursor,
        )
    if rows:
        paper_ids = [int(row["id"]) for row in rows]
        async with pool.acquire() as conn:
            await assert_papers_ownership(conn, paper_ids, user_id)
    return rows


async def _run_library_page(
    rows: list[Any],
    run: _LibraryRun,
    ctx: ProgressContext,
    total: int,
    progress: _LibraryProgress,
) -> bool:
    """Run one page and return whether cancellation stopped it early."""
    for row in rows:
        if await ctx.is_cancelled():
            return True
        paper_id = int(row["id"])
        await ctx.update_progress(
            progress.examined / total,
            f"Paper {paper_id} ({progress.examined + 1}/{total})",
        )
        sub_ctx = _SubCtx(
            ctx,
            (progress.examined / total) * 0.95,
            ((progress.examined + 1) / total) * 0.95,
        )
        progress.record(await _run_library_paper_stages(row, run, sub_ctx))
    return False


async def _finish_library_run(
    ctx: ProgressContext,
    total: int,
    progress: _LibraryProgress,
    *,
    cancelled: bool,
) -> dict[str, Any]:
    """Publish terminal progress and build the caller-visible result."""
    result = progress.result(total, cancelled=cancelled)
    headline = "Done" if result["status"] == "ok" else result["status"].title()
    await ctx.update_progress(
        1.0,
        f"{headline}: {progress.examined}/{total} examined, "
        f"{progress.processed} processed, {progress.summarized} summarized",
    )
    return result


async def _papers_process_library_job(
    pool: asyncpg.Pool,
    http_client: httpx.AsyncClient,
    payload: dict[str, Any],
    ctx: ProgressContext,
) -> dict[str, Any]:
    """Process a caller's whole library in one job via a per-paper stage machine.

    Each selected paper runs download (skip for local) → process (when
    missing chunks) or bounded vector reconciliation → summarize (opt-in, when
    no summary exists). Each
    paper's stages are isolated: a failure records an ``errors`` entry and the
    loop continues; a paper with no PDF source records a ``blocked`` entry (a
    skip, not an error) for the PDF-dependent stages only. The result ``status``
    is ``"partial"`` whenever any paper failed OR was blocked, so an all-blocked
    run never reads as success, and ``"cancelled"`` when the run stopped early on
    cancellation, so a run that never reached most of its papers cannot read as a
    clean completion.
    Reruns are idempotent: completed work is verified and reused, while only
    missing or stale stages mutate state.

    Payload keys:
        user_id (int): REQUIRED — the library owner and tenancy boundary.
        summarize (bool): also generate missing summaries.
    """
    user_id: int | None = payload.get("user_id")
    if user_id is None:
        raise JobError("papers.process_library requires a user_id")
    summarize: bool = bool(payload.get("summarize", False))

    async with pool.acquire() as conn:
        total = int(await conn.fetchval(_PROCESS_LIBRARY_COUNT, user_id) or 0)
    if total == 0:
        await ctx.update_progress(1.0, "Library already processed")
        return _LibraryProgress().result(total, cancelled=False)

    run = _resolve_library_run(pool, http_client, user_id, summarize)
    progress = _LibraryProgress()
    cancelled = False
    await ctx.update_progress(0.0, f"Starting: {total} papers")
    cursor = 0
    while progress.examined < total:
        rows = await _fetch_library_page(pool, user_id, summarize, cursor)
        if not rows:
            break
        cancelled = await _run_library_page(rows, run, ctx, total, progress)
        if cancelled:
            break
        cursor = int(rows[-1]["id"])
    return await _finish_library_run(ctx, total, progress, cancelled=cancelled)


async def _papers_scan_local_job(
    pool: asyncpg.Pool,
    http_client: httpx.AsyncClient,
    payload: dict[str, Any],
    ctx: ProgressContext,
) -> dict[str, Any]:
    """Scan the local PDF drop directory and import new PDFs."""
    from paper_ingestion.services.local_pdfs import scan_local_pdf_directory

    user_id: int | None = payload.get("user_id")
    await ctx.update_progress(0.05, "Scanning local PDF directory")
    result = await scan_local_pdf_directory(
        pool,
        user_id=user_id,
        scan_dir=payload.get("scan_dir"),
    )
    await ctx.update_progress(1.0, "Done")
    return result


# ---------------------------------------------------------------------------
# papers.batch_summarize handler
# ---------------------------------------------------------------------------


async def _papers_batch_summarize_job(
    pool: asyncpg.Pool,
    http_client: httpx.AsyncClient,
    payload: dict[str, Any],
    ctx: ProgressContext,
) -> dict[str, Any]:
    """Summarize many papers in a single background job.

    Payload keys:
        paper_ids (list[int]): DB paper IDs to summarize.
    """
    from paper_ingestion.services.summarization import generate_paper_summary

    paper_ids: list[int] = list(payload.get("paper_ids", []))
    user_id: int | None = payload.get("user_id")
    total = len(paper_ids)
    if user_id is not None:
        async with pool.acquire() as conn:
            await assert_papers_ownership(conn, paper_ids, user_id)

    services = get_services()
    verifier = services.verifier
    if verifier is None:
        raise RuntimeError("verifier not initialized")
    embedder = services.embedder
    if embedder is None:
        raise RuntimeError("embedder not initialized")

    summarized = 0
    failed = 0
    errors: list[str] = []
    cancelled = False

    await ctx.update_progress(0.0, f"Starting: {total} papers")
    for i, paper_id in enumerate(paper_ids):
        if await ctx.is_cancelled():
            cancelled = True
            break
        frac = (i / max(total, 1)) * 0.95
        await ctx.update_progress(frac, f"Summarizing paper {paper_id} ({i + 1}/{total})")
        try:
            await generate_paper_summary(
                paper_id, pool, http_client, verifier, embedder, user_id=user_id
            )
            summarized += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            errors.append(f"Paper {paper_id}: {classify_bulk_error(exc)}")
            logger.exception("Batch summarize failed for paper %s", paper_id)

    remaining = max(total - summarized - failed, 0)
    status = batch_terminal_status(cancelled=cancelled, incomplete=bool(failed or remaining))
    headline = "Done" if status == "ok" else status.title()
    await ctx.update_progress(1.0, f"{headline}: {summarized} ok, {failed} failed")
    return {
        "status": status,
        "total": total,
        "summarized": summarized,
        "failed": failed,
        "remaining": remaining,
        "errors": errors,
    }


async def _digest_weekly_job(
    pool: asyncpg.Pool,
    http_client: httpx.AsyncClient,
    payload: dict[str, Any],
    ctx: ProgressContext,
) -> dict[str, Any]:
    """Generate the weekly digest in a visible durable job."""
    from paper_ingestion.weekly_summary import generate_weekly_summary

    services = get_services()
    verifier = services.verifier
    if verifier is None:
        raise RuntimeError("verifier not initialized")
    days = int(payload.get("days", 7))
    user_id: int | None = payload.get("user_id")
    await ctx.update_progress(0.1, "Generating weekly digest")
    # weekly_summary uses openai_client directly; http_client kept on the
    # jobs-router signature for backwards compat with other handlers.
    _ = http_client
    digest = await generate_weekly_summary(
        pool,
        days=days,
        verifier=verifier,
        user_id=user_id,
        openai_client=services.openai_client,
    )
    await ctx.update_progress(1.0, "Done")
    return digest
