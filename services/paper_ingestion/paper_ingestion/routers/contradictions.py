"""API routes for verified cross-paper contradiction detection."""

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
from paper_ingestion.models import (
    ConsensusResponse,
    ContradictionListResponse,
    ContradictionScanRequest,
)
from paper_ingestion.services.contradictions import (
    aggregate_consensus,
    count_scannable_summaries,
    list_contradictions,
)
from paper_ingestion.services.job_enqueue import enqueue_job

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
    user_id: int = Depends(current_user_id_strict),
) -> ContradictionListResponse:
    """List persisted quote-verified contradictions."""
    async with db_pool.acquire() as conn:
        contradictions, total = await list_contradictions(
            conn,
            user_id=user_id,
            paper_id=paper_id,
            status=status,
            limit=limit,
        )
    return ContradictionListResponse(contradictions=contradictions, total=total)


@router.get("/consensus", response_model=ConsensusResponse)
@limiter.limit("30/minute")
async def get_consensus(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(current_user_id_strict),
) -> ConsensusResponse:
    """Aggregate supports/opposes per shared claim across the caller's library."""
    async with db_pool.acquire() as conn:
        claims, truncated = await aggregate_consensus(conn, user_id=user_id, limit=limit)
    return ConsensusResponse(claims=claims, total=len(claims), truncated=truncated)


@router.post("/contradictions/scan", response_model=JobCreateResponse, status_code=202)
@limiter.limit("5/minute")
async def scan_contradictions(
    request: Request,
    body: ContradictionScanRequest | None = Body(default=None),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(current_user_id_strict),
) -> JobCreateResponse:
    """Enqueue a bounded cross-paper contradiction scan.

    Preflight: when the caller has no summarized papers with findings, the
    scan is guaranteed to be empty, so respond ``status="skipped"`` (job_id
    null, ``reason="no_findings"``) instead of queuing a no-op job.
    """
    async with db_pool.acquire() as conn:
        if await count_scannable_summaries(conn, user_id=user_id) == 0:
            return JobCreateResponse(job_id=None, status="skipped", reason="no_findings")
    payload = body.model_dump() if body else {"paper_id": None, "limit": 25}
    return await enqueue_job(KIND_TO_TASK["contradictions.scan"], user_id=user_id, **payload)


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
    user_id: int = Depends(current_user_id_strict),
) -> JobCreateResponse:
    """Enqueue a contradiction scan focused on one paper."""
    async with db_pool.acquire() as conn:
        await assert_paper_ownership(conn, paper_id, user_id)
    limit = body.limit if body else 25
    return await enqueue_job(
        KIND_TO_TASK["contradictions.scan"], user_id=user_id, paper_id=paper_id, limit=limit
    )
