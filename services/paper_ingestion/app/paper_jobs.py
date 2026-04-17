"""Job handlers for paper ingestion ops: paper.process and paper.analyze.

These handlers are registered with the jarvis_common jobs backbone so the
worker_loop can pick them up.  They must be imported at startup (see main.py)
so that the @job_handler decorators execute and populate _HANDLERS.

Handler signature: async (pool, http_client, payload, ctx) -> dict
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx
from jarvis_common.jobs import JobContext, JobError, job_handler

from app.pdf_processor import PDF_STORAGE_PATH

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# _SubCtx — progress scaling helper
# ---------------------------------------------------------------------------


class _SubCtx:
    """Wraps a JobContext and scales inner progress (0–1) to an outer range.

    Example: ``_SubCtx(ctx, 0.2, 0.7)`` maps inner 0 → 0.2 and inner 1 → 0.7.
    """

    def __init__(self, ctx: JobContext, start: float, end: float) -> None:
        self._ctx = ctx
        self._start = start
        self._end = end

    async def update_progress(self, progress: float, message: str | None = None) -> None:
        scaled = self._start + progress * (self._end - self._start)
        await self._ctx.update_progress(scaled, message)

    async def is_cancelled(self) -> bool:
        return await self._ctx.is_cancelled()


# ---------------------------------------------------------------------------
# paper.process handler
# ---------------------------------------------------------------------------


@job_handler("paper.process")
async def _paper_process_job(
    pool: Any,
    http_client: httpx.AsyncClient,
    payload: dict[str, Any],
    ctx: JobContext,
) -> dict[str, Any]:
    """Process a downloaded paper's PDF: extract, chunk, embed.

    Payload keys:
        paper_id (int): DB paper ID — PDF must already be downloaded.
        force (bool): re-process even if chunks already exist.
    """
    from app.main import app as _app  # lazy import avoids circular at module load
    from app.services.pdf_workflow import run_process_pdf

    paper_id: int = payload["paper_id"]
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
    if not pdf_path.resolve().is_relative_to(Path(PDF_STORAGE_PATH).resolve()):
        raise JobError(f"Invalid PDF path for paper {paper_id}")
    if not pdf_path.exists():
        raise JobError(f"PDF file missing from disk for paper {paper_id}")

    await ctx.update_progress(0.1, "Downloaded")

    pdf_processor = _app.state.pdf_processor
    embedder = _app.state.embedder

    result = await run_process_pdf(
        paper_id,
        pdf_path,
        pool,
        pdf_processor,
        embedder,
        force=force,
    )

    await ctx.update_progress(1.0, "Done")
    return result


# ---------------------------------------------------------------------------
# paper.analyze handler (composite: download → process → summarize)
# ---------------------------------------------------------------------------


@job_handler("paper.analyze")
async def _paper_analyze_job(
    pool: Any,
    http_client: httpx.AsyncClient,
    payload: dict[str, Any],
    ctx: JobContext,
) -> dict[str, Any]:
    """Chain download → process → summarize for a single paper.

    Payload keys:
        paper_id (int): DB paper ID.

    B6 fix: local papers (source_type='local' or pdf_local_path IS NOT NULL)
    skip the download step.
    """
    from app.main import app as _app  # lazy import avoids circular at module load
    from app.services.pdf_workflow import run_process_pdf
    from app.services.summarization import generate_paper_summary

    paper_id: int = payload["paper_id"]

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

    pdf_processor = _app.state.pdf_processor
    embedder = _app.state.embedder
    verifier = _app.state.verifier

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
    else:
        await ctx.update_progress(0.0, "Download skipped" if is_local else "Already downloaded")

    # ---- Step 2: Process PDF ----
    await ctx.update_progress(0.2, "Processing PDF")

    pdf_local_path = row["pdf_local_path"]
    if not pdf_local_path:
        raise JobError(f"PDF path not set for paper {paper_id}")

    pdf_path = Path(pdf_local_path)
    if not pdf_path.resolve().is_relative_to(Path(PDF_STORAGE_PATH).resolve()):
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
    )

    await ctx.update_progress(1.0, "Done")
    return {
        "paper_id": paper_id,
        "chunk_count": result.get("chunk_count", 0),
        "process_status": result.get("status"),
    }
