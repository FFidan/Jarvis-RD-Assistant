"""API routes for verified cross-paper contradiction detection."""

import uuid

import asyncpg
from fastapi import APIRouter, Body, Depends, Query, Request
from jarvis_common import (
    ErrorResponse,
    JobCreateResponse,
    assert_paper_ownership,
    current_user_id_strict,
)
from jarvis_common.task_registry import KIND_TO_TASK

from paper_ingestion.deps import get_db_pool, limiter
from paper_ingestion.models import ContradictionListResponse, ContradictionScanRequest
from paper_ingestion.services.contradictions import list_contradictions

router = APIRouter(
    prefix="/api",
    tags=["contradictions"],
    responses={
        400: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
)


@router.get("/contradictions", response_model=ContradictionListResponse)
@limiter.limit("30/minute")
async def get_contradictions(
    request: Request,
    paper_id: int | None = Query(default=None, ge=1),
    status: str | None = Query(default="verified", pattern="^(verified|dismissed|false_positive)$"),
    limit: int = Query(default=20, ge=1, le=100),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> ContradictionListResponse:
    """List persisted quote-verified contradictions."""
    user_id = await current_user_id_strict(request)
    async with db_pool.acquire() as conn:
        contradictions, total = await list_contradictions(
            conn,
            user_id=user_id,
            paper_id=paper_id,
            status=status,
            limit=limit,
        )
    return ContradictionListResponse(contradictions=contradictions, total=total)


@router.post("/contradictions/scan", response_model=JobCreateResponse, status_code=202)
@limiter.limit("5/minute")
async def scan_contradictions(
    request: Request,
    body: ContradictionScanRequest | None = Body(default=None),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> JobCreateResponse:
    """Enqueue a bounded cross-paper contradiction scan."""
    user_id = await current_user_id_strict(request)
    payload = body.model_dump() if body else {"paper_id": None, "limit": 25}
    jarvis_job_id = str(uuid.uuid4())
    await KIND_TO_TASK["contradictions.scan"].defer_async(
        job_id=jarvis_job_id, user_id=user_id, **payload
    )
    return JobCreateResponse(job_id=jarvis_job_id, status="queued")


@router.post(
    "/papers/{paper_id}/contradictions/scan",
    response_model=JobCreateResponse,
    status_code=202,
)
@limiter.limit("5/minute")
async def scan_paper_contradictions(
    request: Request,
    paper_id: int,
    body: ContradictionScanRequest | None = Body(default=None),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> JobCreateResponse:
    """Enqueue a contradiction scan focused on one paper."""
    user_id = await current_user_id_strict(request)
    async with db_pool.acquire() as conn:
        await assert_paper_ownership(conn, paper_id, user_id)
    limit = body.limit if body else 25
    jarvis_job_id = str(uuid.uuid4())
    await KIND_TO_TASK["contradictions.scan"].defer_async(
        job_id=jarvis_job_id, user_id=user_id, paper_id=paper_id, limit=limit
    )
    return JobCreateResponse(job_id=jarvis_job_id, status="queued")
