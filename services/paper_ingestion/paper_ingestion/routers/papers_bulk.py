"""Bulk and batch-process endpoints: bulk_action_papers, process_batch."""

import logging
import uuid

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request
from jarvis_common import ErrorResponse, JobCreateResponse, assert_papers_ownership
from jarvis_common.auth import get_current_user_id

from paper_ingestion import papers_service
from paper_ingestion.deps import get_db_pool, limiter
from paper_ingestion.models import (
    BulkActionRequest,
    BulkActionResponse,
    ProcessBatchRequest,
)

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


def _classify_bulk_error(exc: Exception) -> str:
    """Map exceptions to safe, operator-diagnostic response codes.

    Raw exception messages (asyncpg constraint names, SQL text) are never
    forwarded to the caller — only the code string is returned.
    """
    if isinstance(exc, HTTPException):
        if exc.status_code == 404:
            return "not_found"
        if exc.status_code == 403:
            return "forbidden"
        if exc.status_code == 409:
            return "conflict"
        return "http_error"
    if isinstance(exc, asyncpg.UniqueViolationError):
        return "already_in_state"
    if isinstance(exc, asyncpg.ForeignKeyViolationError):
        return "not_found"
    if isinstance(exc, asyncpg.NotNullViolationError | asyncpg.CheckViolationError):
        return "constraint_error"
    if isinstance(exc, asyncpg.PostgresError):
        return "db_error"
    if isinstance(exc, ValueError):
        return "invalid_action"
    return "unknown_error"


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
                    failed.append({"paper_id": paper_id, "error": _classify_bulk_error(exc)})

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


@router.post("/process_batch", response_model=JobCreateResponse)
@limiter.limit("10/minute")
async def process_batch(
    request: Request,
    body: ProcessBatchRequest,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(get_current_user_id),
) -> dict[str, str]:
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

    jarvis_job_id = str(uuid.uuid4())
    await KIND_TO_TASK["papers.batch_process"].defer_async(
        job_id=jarvis_job_id, user_id=user_id, paper_ids=body.paper_ids
    )
    return {"job_id": jarvis_job_id, "status": "queued"}
