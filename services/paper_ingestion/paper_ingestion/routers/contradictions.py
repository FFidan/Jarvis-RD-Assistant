"""API routes for verified cross-paper contradiction detection."""

import asyncpg
from fastapi import APIRouter, Body, Depends, Query, Request
from jarvis_common import ErrorResponse, JobCreateResponse
from jarvis_common import jobs as jobs_lib

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
    async with db_pool.acquire() as conn:
        contradictions, total = await list_contradictions(
            conn,
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
    payload = body.model_dump() if body else {"paper_id": None, "limit": 25}
    job_id = await jobs_lib.enqueue(db_pool, "contradictions.scan", payload)
    return JobCreateResponse(job_id=job_id, status="queued")


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
    limit = body.limit if body else 25
    job_id = await jobs_lib.enqueue(
        db_pool,
        "contradictions.scan",
        {"paper_id": paper_id, "limit": limit},
    )
    return JobCreateResponse(job_id=job_id, status="queued")
