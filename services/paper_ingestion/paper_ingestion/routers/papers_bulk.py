"""Bulk and batch-process endpoints: bulk_action_papers, process_batch."""

import logging

import asyncpg
from fastapi import APIRouter, Depends, Request
from jarvis_common import ErrorResponse, JobCreateResponse, assert_papers_ownership
from jarvis_common.auth import get_current_user_id

from paper_ingestion import papers_service
from paper_ingestion.deps import get_db_pool, limiter
from paper_ingestion.job_errors import classify_bulk_error
from paper_ingestion.models import (
    BulkActionRequest,
    BulkActionResponse,
    ProcessBatchRequest,
)
from paper_ingestion.services.job_enqueue import enqueue_job

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/api/papers",
    tags=["papers"],
    responses={
        400: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
)


# ---------------------------------------------------------------------------
# POST /api/papers/bulk
# ---------------------------------------------------------------------------


@router.post("/bulk", response_model=BulkActionResponse)
@limiter.limit("10/minute")
async def bulk_action_papers(
    request: Request,
    body: BulkActionRequest,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(get_current_user_id),
) -> dict[str, object]:
    """Apply a lifecycle action to multiple papers atomically.

    Returns ``{"succeeded": [...], "failed": [{"paper_id": int, "error": str}]}``.
    Partial failures are collected; the outer transaction is committed even when
    individual papers fail (per-paper savepoints isolate rollbacks).
    """
    succeeded: list[int] = []
    failed: list[dict[str, object]] = []
    hard_deleted_ids: list[int] = []

    async with db_pool.acquire() as conn:
        async with conn.transaction():
            for paper_id in body.paper_ids:
                try:
                    async with conn.transaction():
                        await papers_service.assert_paper_ownership(conn, paper_id, user_id)
                        await papers_service._apply_bulk_action(
                            conn,
                            paper_id,
                            user_id,
                            body.action,
                            _hard_deleted_ids=hard_deleted_ids,
                        )
                    succeeded.append(paper_id)
                except Exception as exc:  # noqa: BLE001
                    logger.exception(
                        "bulk_action_papers: paper_id=%d action=%s failed",
                        paper_id,
                        body.action,
                    )
                    failed.append({"paper_id": paper_id, "error": classify_bulk_error(exc)})

    for pid in hard_deleted_ids:
        try:
            await papers_service.delete_paper_vectors(pid)
        except Exception:  # noqa: BLE001
            logger.exception(
                "Qdrant cleanup failed for paper %d after bulk hard_delete; "
                "vectors are now orphans",
                pid,
            )

    return {"succeeded": succeeded, "failed": failed}


# ---------------------------------------------------------------------------
# POST /api/papers/process_batch
# ---------------------------------------------------------------------------


@router.post("/process_batch", response_model=JobCreateResponse, status_code=202)
@limiter.limit("10/minute")
async def process_batch(
    request: Request,
    body: ProcessBatchRequest,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(get_current_user_id),
) -> JobCreateResponse:
    """Enqueue a ``papers.batch_process`` job for the given paper IDs.

    Accepts 1–50 explicit paper IDs and immediately queues the job without
    any pre-flight filtering.  The caller can poll progress via
    ``GET /api/jobs/{job_id}``.

    Returns ``{"job_id": "<uuid>", "status": "queued"}``.
    """
    _ = request  # required by @limiter.limit; not used in body
    from jarvis_common.task_registry import KIND_TO_TASK  # noqa: PLC0415

    async with db_pool.acquire() as conn:
        await assert_papers_ownership(conn, list(body.paper_ids), user_id)

    return await enqueue_job(
        KIND_TO_TASK["papers.batch_process"], user_id=user_id, paper_ids=body.paper_ids
    )


# ---------------------------------------------------------------------------
# POST /api/papers/process-library
# ---------------------------------------------------------------------------


@router.post("/process-library", response_model=JobCreateResponse, status_code=202)
@limiter.limit("2/minute")
async def process_library(
    request: Request,
    summarize: bool = False,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(get_current_user_id),
) -> JobCreateResponse:
    """Enqueue one ``papers.process_library`` job for the caller's whole library.

    The job processes the caller's ``user_library`` in bounded pages for download,
    processing, vector reconciliation, and opt-in summaries. Completed papers stay
    eligible for a cheap Qdrant identity probe because PostgreSQL alone cannot prove
    their vectors still match. An empty library returns the skip contract
    ``{"job_id": null, "status": "skipped", "reason":
    "library_already_processed"}``. Poll progress via ``GET /api/jobs/{job_id}``.
    """
    _ = request  # required by @limiter.limit; not used in body
    from jarvis_common.task_registry import KIND_TO_TASK  # noqa: PLC0415

    from paper_ingestion.paper_jobs import (  # noqa: PLC0415
        _PROCESS_LIBRARY_PAGE_SIZE,
        _PROCESS_LIBRARY_SELECTION,
    )

    async with db_pool.acquire() as conn:
        has_work = await conn.fetchval(
            f"SELECT EXISTS ({_PROCESS_LIBRARY_SELECTION})",
            user_id,
            summarize,
            _PROCESS_LIBRARY_PAGE_SIZE,
            0,
        )
    if not has_work:
        return JobCreateResponse(job_id=None, status="skipped", reason="library_already_processed")

    return await enqueue_job(
        KIND_TO_TASK["papers.process_library"], user_id=user_id, summarize=summarize
    )
