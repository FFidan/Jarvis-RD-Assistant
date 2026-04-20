"""Compound Analyze Paper endpoint — SSE streaming of download → process → summarize."""

import json
import logging
import os
from pathlib import Path

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from starlette.responses import StreamingResponse

from app.deps import get_db_pool, limiter

logger = logging.getLogger(__name__)
router = APIRouter(tags=["analyze"])


def _sanitize_sse_error(exc: Exception) -> str:
    """Return a safe error message that doesn't leak implementation details.

    Only passes through messages from known safe exception types (ValueError,
    HTTPException). All other exceptions return a generic message.
    """
    if isinstance(exc, HTTPException):
        return str(exc.detail)
    if isinstance(exc, ValueError):
        return str(exc)
    return "Analysis failed. Please try again."


def _sse_event(data: dict | str) -> str:
    """Format a single SSE frame."""
    if isinstance(data, str):
        return f"data: {data}\n\n"
    return f"data: {json.dumps(data)}\n\n"


async def _analyze_stream(request: Request, paper_id: int, db_pool: asyncpg.Pool):
    """Async generator: download → process → summarize with SSE progress events.

    B6 fix: local papers (``source_type='local'`` or ``pdf_local_path`` already
    set) skip the download step entirely — they never have a ``pdf_url`` and
    that is expected, not an error.
    """
    http_client = request.app.state.http_client
    pdf_processor = request.app.state.pdf_processor
    embedder = request.app.state.embedder

    # ---- Step 1: Download PDF ----
    yield _sse_event({"type": "step", "step": "downloading", "status": "started"})
    try:
        # Phase 1a: Check paper state (short query, no lock)
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, source_type, pdf_url, pdf_downloaded, pdf_local_path "
                "FROM papers WHERE id = $1",
                paper_id,
            )
        if not row:
            yield _sse_event({"type": "error", "step": "downloading", "message": "Paper not found"})
            yield _sse_event("[DONE]")
            return

        # B6: local papers skip the download step — they already have a pdf_local_path
        is_local = row["source_type"] == "local" or row["pdf_local_path"] is not None
        if not is_local and not row["pdf_url"]:
            yield _sse_event(
                {"type": "error", "step": "downloading", "message": "Paper has no PDF URL"}
            )
            yield _sse_event("[DONE]")
            return

        if is_local:
            # Local paper: skip download, emit skipped event
            yield _sse_event(
                {
                    "type": "step",
                    "step": "downloading",
                    "status": "skipped",
                    "reason": "local paper",
                }
            )
        elif row["pdf_downloaded"]:
            # Already downloaded: nothing to do
            yield _sse_event({"type": "step", "step": "downloading", "status": "completed"})
        else:
            # Phase 1b: Download outside any transaction
            pdf_path = await pdf_processor.download_pdf(row["pdf_url"], paper_id)
            # Phase 1c: Update DB (short query)
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    "UPDATE papers SET pdf_local_path = $1, pdf_downloaded = TRUE "
                    "WHERE id = $2 RETURNING id, source_type, pdf_url, pdf_downloaded,"
                    " pdf_local_path",
                    str(pdf_path),
                    paper_id,
                )
            yield _sse_event({"type": "step", "step": "downloading", "status": "completed"})
    except Exception as exc:
        logger.error("Download failed for paper %d: %s", paper_id, exc)
        yield _sse_event({"type": "error", "step": "downloading", "message": "PDF download failed"})
        yield _sse_event("[DONE]")
        return

    # ---- Step 2: Process PDF ----
    yield _sse_event({"type": "step", "step": "processing", "status": "started"})
    try:
        pdf_local_path = row["pdf_local_path"]
        if not pdf_local_path:
            yield _sse_event(
                {
                    "type": "error",
                    "step": "processing",
                    "message": "PDF path not set despite download flag",
                }
            )
            yield _sse_event("[DONE]")
            return
        pdf_path = Path(pdf_local_path)
        pdf_storage = os.environ.get("PDF_STORAGE_PATH", "/data/pdfs")
        if not pdf_path.resolve().is_relative_to(Path(pdf_storage).resolve()):
            yield _sse_event({"type": "error", "step": "processing", "message": "Invalid PDF path"})
            yield _sse_event("[DONE]")
            return
        if not pdf_path.exists():
            yield _sse_event(
                {"type": "error", "step": "processing", "message": "PDF file missing from disk"}
            )
            yield _sse_event("[DONE]")
            return

        from app.services.pdf_workflow import run_process_pdf

        result = await run_process_pdf(paper_id, pdf_path, db_pool, pdf_processor, embedder)
        chunk_count = result.get("chunk_count", 0)
    except Exception as exc:
        logger.error("Processing failed for paper %d: %s", paper_id, exc)
        yield _sse_event(
            {"type": "error", "step": "processing", "message": "PDF processing failed"}
        )
        yield _sse_event("[DONE]")
        return

    yield _sse_event(
        {
            "type": "step",
            "step": "processing",
            "status": "completed",
            "chunk_count": chunk_count,
        }
    )

    # ---- Step 3: Summarize ----
    yield _sse_event({"type": "step", "step": "summarizing", "status": "started"})
    try:
        # Call core summarization logic directly (bypasses rate limiter)
        from app.services.summarization import generate_paper_summary

        await generate_paper_summary(
            paper_id,
            db_pool,
            http_client,
            request.app.state.verifier,
            embedder,
        )
    except Exception as exc:
        logger.error("Summarization failed for paper %d: %s", paper_id, exc)
        yield _sse_event(
            {
                "type": "error",
                "step": "summarizing",
                "message": _sanitize_sse_error(exc),
            }
        )
        yield _sse_event("[DONE]")
        return

    yield _sse_event({"type": "step", "step": "summarizing", "status": "completed"})
    yield _sse_event({"type": "complete", "paper_id": paper_id})
    yield _sse_event("[DONE]")


@router.post("/api/papers/{paper_id}/analyze")
@limiter.limit("5/minute")
async def analyze_paper(
    request: Request,
    paper_id: int,
    async_mode: bool = Query(default=False, alias="async"),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
):
    """Chain download → process → summarize with SSE progress events.

    Default (no ``?async=true``): returns a streaming ``text/event-stream`` response.
    Each step emits ``started`` / ``completed`` events; on error emits a single
    ``error`` event and terminates.

    With ``?async=true``: enqueues a ``paper.analyze`` job and returns
    ``{"job_id": "...", "status": "queued"}`` immediately.
    """
    if async_mode:
        from jarvis_common.jobs import enqueue

        job_id = await enqueue(db_pool, "paper.analyze", {"paper_id": paper_id})
        return {"job_id": job_id, "status": "queued"}

    return StreamingResponse(
        _analyze_stream(request, paper_id, db_pool),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
