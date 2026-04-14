"""Compound Analyze Paper endpoint — SSE streaming of download → process → summarize."""

import json
import logging
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from starlette.responses import StreamingResponse

from app.deps import limiter

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


async def _analyze_stream(request: Request, paper_id: int):
    """Async generator: download → process → summarize with SSE progress events."""
    db_pool = request.app.state.db_pool
    http_client = request.app.state.http_client
    pdf_processor = request.app.state.pdf_processor
    embedder = request.app.state.embedder

    # ---- Step 1: Download PDF ----
    yield _sse_event({"type": "step", "step": "downloading", "status": "started"})
    try:
        # Phase 1a: Check paper state (short query, no lock)
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM papers WHERE id = $1", paper_id)
        if not row:
            yield _sse_event({"type": "error", "step": "downloading", "message": "Paper not found"})
            yield _sse_event("[DONE]")
            return
        if not row["pdf_url"]:
            yield _sse_event(
                {"type": "error", "step": "downloading", "message": "Paper has no PDF URL"}
            )
            yield _sse_event("[DONE]")
            return

        # Phase 1b: Download outside any transaction
        if not row["pdf_downloaded"]:
            pdf_path = await pdf_processor.download_pdf(row["pdf_url"], paper_id)
            # Phase 1c: Update DB (short query)
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    "UPDATE papers SET pdf_local_path = $1, pdf_downloaded = TRUE "
                    "WHERE id = $2 RETURNING *",
                    str(pdf_path),
                    paper_id,
                )
    except Exception as exc:
        logger.error("Download failed for paper %d: %s", paper_id, exc)
        yield _sse_event({"type": "error", "step": "downloading", "message": "PDF download failed"})
        yield _sse_event("[DONE]")
        return

    yield _sse_event({"type": "step", "step": "downloading", "status": "completed"})

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
async def analyze_paper(request: Request, paper_id: int):
    """Chain download → process → summarize with SSE progress events.

    Returns a streaming response with ``text/event-stream`` content type.
    Each step emits ``started`` / ``completed`` events.  On error the stream
    includes a single ``error`` event and terminates.
    """
    return StreamingResponse(
        _analyze_stream(request, paper_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
