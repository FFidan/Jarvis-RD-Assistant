"""Job handlers for paper ingestion ops: paper.process and paper.analyze.

These handlers are called by procrastinate task wrappers in task_registry.py.

Handler signature: async (pool, http_client, payload, ctx) -> dict
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, cast

import asyncpg
import httpx
from jarvis_common.db_helpers import assert_paper_ownership, assert_papers_ownership
from jarvis_common.jobs import JobError, ProgressContext

from paper_ingestion._state import get_services
from paper_ingestion.pdf_processor import PDF_STORAGE_PATH, check_pdf_path_safe

logger = logging.getLogger(__name__)

__all__ = [
    "_SubCtx",
    "PDF_STORAGE_PATH",
    "_paper_process_job",
    "_paper_analyze_job",
    "_paper_summarize_job",
    "_papers_batch_process_job",
    "_papers_batch_summarize_job",
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
    """
    from paper_ingestion.services.pdf_workflow import run_process_pdf

    paper_id: int = payload["paper_id"]
    user_id: int | None = payload.get("user_id")
    async with pool.acquire() as conn:
        await assert_paper_ownership(conn, paper_id, user_id)

    force: bool = bool(payload.get("force", False))

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

    pdf_path = Path(row["pdf_local_path"])
    if not check_pdf_path_safe(pdf_path, PDF_STORAGE_PATH):
        raise JobError(f"Invalid PDF path for paper {paper_id}")
    if not pdf_path.exists():
        raise JobError(f"PDF file missing from disk for paper {paper_id}")

    await ctx.update_progress(0.1, "Downloaded")

    services = get_services()
    pdf_processor = services.pdf_processor
    if pdf_processor is None:
        raise RuntimeError("pdf_processor not initialized")
    embedder = services.embedder
    if embedder is None:
        raise RuntimeError("embedder not initialized")

    result = await run_process_pdf(
        paper_id,
        pdf_path,
        pool,
        pdf_processor,
        embedder,
        force=force,
        ctx=_SubCtx(ctx, 0.1, 1.0),
    )
    return cast(dict[str, Any], result)


# ---------------------------------------------------------------------------
# paper.analyze handler (composite: download → process → summarize)
# ---------------------------------------------------------------------------


async def _paper_analyze_job(
    pool: asyncpg.Pool,
    http_client: httpx.AsyncClient,
    payload: dict[str, Any],
    ctx: ProgressContext,
) -> dict[str, Any]:
    """Chain download → process → summarize for a single paper.

    Payload keys:
        paper_id (int): DB paper ID.

    B6 fix: local papers (source_type='local' or pdf_local_path IS NOT NULL)
    skip the download step.
    """
    from paper_ingestion.services.pdf_workflow import run_process_pdf
    from paper_ingestion.services.summarization import generate_paper_summary

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
    if not is_local and not row["pdf_downloaded"]:
        await ctx.update_progress(0.0, "Downloading PDF")
        pdf_path_obj = await pdf_processor.download_pdf(row["pdf_url"], paper_id)
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE papers SET pdf_local_path = $1, pdf_downloaded = TRUE "
                "WHERE id = $2 RETURNING id, source_type, pdf_url, pdf_downloaded,"
                " pdf_local_path",
                str(pdf_path_obj),
                paper_id,
            )
        # PI-CORR-01: the row can be deleted between the initial load and this
        # post-download UPDATE (TOCTOU). UPDATE ... RETURNING then yields None;
        # dereferencing it below would raise an opaque TypeError. Fail cleanly.
        if row is None:
            raise JobError(f"Paper {paper_id} deleted during download")
    else:
        await ctx.update_progress(0.0, "Download skipped" if is_local else "Already downloaded")

    # ---- Step 2: Process PDF ----
    await ctx.update_progress(0.2, "Processing PDF")

    pdf_local_path = row["pdf_local_path"]
    if not pdf_local_path:
        raise JobError(f"PDF path not set for paper {paper_id}")

    pdf_path = Path(pdf_local_path)
    if not check_pdf_path_safe(pdf_path, PDF_STORAGE_PATH):
        raise JobError(f"Invalid PDF path for paper {paper_id}")
    if not pdf_path.exists():
        raise JobError(f"PDF file missing from disk for paper {paper_id}")

    sub_ctx = _SubCtx(ctx, 0.2, 0.7)
    result = await run_process_pdf(
        paper_id,
        pdf_path,
        pool,
        pdf_processor,
        embedder,
        ctx=sub_ctx,
    )

    # ---- Step 3: Summarize ----
    await ctx.update_progress(0.7, "Summarizing")
    await generate_paper_summary(
        paper_id,
        pool,
        http_client,
        verifier,
        embedder,
        user_id=user_id,
        force=force,
    )

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
    from paper_ingestion.services.summarization import generate_paper_summary

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
    summary = await generate_paper_summary(
        paper_id, pool, http_client, verifier, embedder, user_id=user_id, force=force
    )
    await ctx.update_progress(1.0, "Done")
    return {"paper_id": paper_id, "summary_id": summary.id, "status": "summarized"}


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

    for i, paper_id in enumerate(paper_ids):
        if await ctx.is_cancelled():
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
            pdf_path = Path(row["pdf_local_path"])
            if not check_pdf_path_safe(pdf_path, PDF_STORAGE_PATH):
                skipped += 1
                continue
            if not pdf_path.exists():
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
            )
            processed += 1
        except Exception as exc:  # noqa: BLE001
            logger.exception("Batch process failed for paper %s", paper_id)
            errors.append(f"Paper {paper_id}: {exc}")

    await ctx.update_progress(1.0, f"Done: {processed} processed, {skipped} skipped")
    return {"processed": processed, "skipped": skipped, "errors": errors}


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

    await ctx.update_progress(0.0, f"Starting: {total} papers")
    for i, paper_id in enumerate(paper_ids):
        if await ctx.is_cancelled():
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
            errors.append(f"Paper {paper_id}: {exc}")
            logger.exception("Batch summarize failed for paper %s", paper_id)

    await ctx.update_progress(1.0, f"Done: {summarized} ok, {failed} failed")
    return {"summarized": summarized, "failed": failed, "errors": errors}


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
