"""Compound Analyze Paper endpoint — SSE streaming of download → process → summarize."""

import logging
from pathlib import Path

import asyncpg
import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from jarvis_common import assert_paper_ownership, current_user_id_strict
from jarvis_common.jobs import JobError
from jarvis_common.sse import SSE_DONE, sse_event
from starlette.responses import StreamingResponse

from paper_ingestion.deps import (
    get_db_pool,
    get_embedder,
    get_http_client,
    get_pdf_processor,
    get_verifier,
    limiter,
)
from paper_ingestion.pdf_processor import check_pdf_path_safe
from paper_ingestion.routers.pdf import _require_library_membership
from paper_ingestion.services.pdf_workflow import download_and_store_pdf

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["analyze"])

PDF_PROCESSING_FAILED_MESSAGE = "PDF processing failed. Check service logs for details."


def safe_sse_error_message(exc: Exception) -> str:
    """Return a safe error message that doesn't leak implementation details.

    Only passes through messages from known safe exception types (ValueError,
    HTTPException, JobError, PDFUserFacingError). All other exceptions return a
    generic message.
    """
    from paper_ingestion.services.pdf_workflow import PDFUserFacingError  # noqa: PLC0415

    if isinstance(exc, HTTPException):
        return str(exc.detail)
    if isinstance(exc, ValueError | JobError | PDFUserFacingError):
        return str(exc)
    return "Analysis failed. Please try again."


async def _analyze_stream(
    request: Request,
    paper_id: int,
    db_pool: asyncpg.Pool,
    http_client: httpx.AsyncClient,
    pdf_processor,
    embedder,
    verifier,
    force: bool = False,
):
    """Async generator: download → process → summarize with SSE progress events.

    B6 fix: local papers (``source_type='local'`` or ``pdf_local_path`` already
    set) skip the download step entirely — they never have a ``pdf_url`` and
    that is expected, not an error.
    """
    # Belt-and-braces ownership check: assert before yielding any data.
    # The primary check lives in analyze_paper (above the if async_mode branch),
    # but this guard protects any future caller that invokes _analyze_stream directly.
    user_id = await current_user_id_strict(request)
    async with db_pool.acquire() as conn:
        await assert_paper_ownership(conn, paper_id, user_id)

    # ---- Step 1: Download PDF ----
    yield sse_event({"type": "step", "step": "downloading", "status": "started"})
    try:
        # Check paper state (short query, no lock)
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, source_type, pdf_url, pdf_downloaded, pdf_local_path "
                "FROM papers WHERE id = $1",
                paper_id,
            )
        if not row:
            yield sse_event({"type": "error", "step": "downloading", "message": "Paper not found"})
            yield SSE_DONE
            return

        # B6: local papers skip the download step — they already have a pdf_local_path
        is_local = row["source_type"] == "local" or row["pdf_local_path"] is not None
        if not is_local and not row["pdf_url"]:
            yield sse_event(
                {"type": "error", "step": "downloading", "message": "Paper has no PDF URL"}
            )
            yield SSE_DONE
            return

        if is_local:
            # Local paper: skip download, emit skipped event
            yield sse_event(
                {
                    "type": "step",
                    "step": "downloading",
                    "status": "skipped",
                    "reason": "local paper",
                }
            )
        elif row["pdf_downloaded"]:
            # Already downloaded: nothing to do
            yield sse_event({"type": "step", "step": "downloading", "status": "completed"})
        else:
            # Download outside any transaction
            row = await download_and_store_pdf(
                db_pool,
                pdf_processor,
                row["pdf_url"],
                paper_id,
            )
            yield sse_event({"type": "step", "step": "downloading", "status": "completed"})
    except Exception as exc:
        logger.error("Download failed for paper %d: %s", paper_id, exc, exc_info=True)
        yield sse_event({"type": "error", "step": "downloading", "message": "PDF download failed"})
        yield SSE_DONE
        return

    # ---- Step 2: Process PDF ----
    yield sse_event({"type": "step", "step": "processing", "status": "started"})
    try:
        pdf_local_path = row["pdf_local_path"]
        if not pdf_local_path:
            yield sse_event(
                {
                    "type": "error",
                    "step": "processing",
                    "message": "PDF path not set despite download flag",
                }
            )
            yield SSE_DONE
            return
        pdf_path = Path(pdf_local_path)
        from paper_ingestion.config import get_paper_ingestion_settings  # noqa: PLC0415

        pdf_storage = get_paper_ingestion_settings().pdf_storage_path
        if not check_pdf_path_safe(pdf_path, pdf_storage):
            yield sse_event({"type": "error", "step": "processing", "message": "Invalid PDF path"})
            yield SSE_DONE
            return
        if not pdf_path.exists():
            yield sse_event(
                {"type": "error", "step": "processing", "message": "PDF file missing from disk"}
            )
            yield SSE_DONE
            return

        from paper_ingestion.services.pdf_workflow import run_process_pdf

        result = await run_process_pdf(
            paper_id,
            pdf_path,
            db_pool,
            pdf_processor,
            embedder,
            force=force,
            requester_id=user_id,
        )
        chunk_count = result.get("chunk_count", 0)
    except Exception:
        logger.exception("Processing failed for paper %d", paper_id)
        yield sse_event(
            {
                "type": "error",
                "step": "processing",
                "message": "PDF processing failed",
                "stage": "process_pdf",
                "error_code": "PDF_PROCESSING_FAILED",
                "display_message": PDF_PROCESSING_FAILED_MESSAGE,
                "error_type": "PDF_PROCESSING_FAILED",
                "error_detail": PDF_PROCESSING_FAILED_MESSAGE,
            }
        )
        yield SSE_DONE
        return

    yield sse_event(
        {
            "type": "step",
            "step": "processing",
            "status": "completed",
            "chunk_count": chunk_count,
        }
    )

    # ---- Step 3: Summarize ----
    yield sse_event({"type": "step", "step": "summarizing", "status": "started"})
    try:
        # Call core summarization logic directly (bypasses rate limiter)
        from paper_ingestion.services.summarization import generate_paper_summary

        await generate_paper_summary(
            paper_id,
            db_pool,
            http_client,
            verifier,
            embedder,
            user_id=user_id,
            force=force,
        )
    except Exception as exc:
        logger.error("Summarization failed for paper %d: %s", paper_id, exc, exc_info=True)
        yield sse_event(
            {
                "type": "error",
                "step": "summarizing",
                "message": safe_sse_error_message(exc),
            }
        )
        yield SSE_DONE
        return

    yield sse_event({"type": "step", "step": "summarizing", "status": "completed"})
    yield sse_event({"type": "complete", "paper_id": paper_id})
    yield SSE_DONE


@router.post("/papers/{paper_id}/analyze", response_model=None)
@limiter.limit("5/minute")
async def analyze_paper(
    request: Request,
    paper_id: int,
    async_mode: bool = Query(default=False, alias="async"),
    force: bool = Query(default=False),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    http_client: httpx.AsyncClient = Depends(get_http_client),
    pdf_processor=Depends(get_pdf_processor),
    embedder=Depends(get_embedder),
    verifier=Depends(get_verifier),
    user_id: int = Depends(current_user_id_strict),
) -> dict[str, str] | StreamingResponse:
    """Chain download → process → summarize with SSE progress events.

    Default (no ``?async=true``): returns a streaming ``text/event-stream`` response.
    Each step emits ``started`` / ``completed`` events; on error emits a single
    ``error`` event and terminates.

    With ``?async=true``: enqueues a ``paper.analyze`` job and returns
    ``{"job_id": "...", "status": "queued"}`` immediately.

    Raises
    ------
    fastapi.HTTPException
        403 when ``force`` is set and the paper is not in the caller's library,
        on either branch: the analysis rebuilds the paper's derived content, so
        it requires holding the paper rather than merely being able to see it.
        The refusal happens before the stream opens, where a status code can
        still be sent.
    """
    async with db_pool.acquire() as conn:
        await assert_paper_ownership(conn, paper_id, user_id)
        if force:
            await _require_library_membership(conn, paper_id, user_id)

    if async_mode:
        import uuid

        from jarvis_common.task_registry import KIND_TO_TASK

        jarvis_job_id = str(uuid.uuid4())
        await KIND_TO_TASK["paper.analyze"].defer_async(
            job_id=jarvis_job_id, user_id=user_id, paper_id=paper_id, force=force
        )
        return {"job_id": jarvis_job_id, "status": "queued"}

    return StreamingResponse(
        _analyze_stream(
            request, paper_id, db_pool, http_client, pdf_processor, embedder, verifier, force=force
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
