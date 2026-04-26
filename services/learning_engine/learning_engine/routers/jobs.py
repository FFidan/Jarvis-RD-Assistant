"""Jobs REST endpoints for learning_engine service.

Provides enqueue, status, list, SSE stream, and cancel endpoints.
The matching worker is wired into the service lifespan in main.py.
"""

from __future__ import annotations

from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from jarvis_common import (
    ErrorResponse,
    JobCreateResponse,
    JobStatusResponse,
    current_user_id,
)
from jarvis_common import jobs as jobs_lib
from jarvis_common.settings import get_jobs_settings
from pydantic import BaseModel, Field, field_validator

from learning_engine.deps import get_db_pool, limiter

# ---------------------------------------------------------------------------
# Allowlist of job kinds that clients may create via POST /api/jobs.
# card.generate_batch is excluded from the public API — it is a large batch
# operation that should only be triggered through the /api/generation/batch
# endpoint with its own validation.
# ---------------------------------------------------------------------------
_BASE_PUBLIC_JOB_KINDS: frozenset[str] = frozenset(
    {
        "card.generate",
    }
)


def _get_public_job_kinds() -> set[str]:
    """Return the set of allowed job kinds, evaluated at request time.

    JARVIS_ENABLE_TEST_JOBS is read on each call so that it can be toggled
    without a restart (e.g. in integration tests that set the env var at runtime).
    """
    kinds = set(_BASE_PUBLIC_JOB_KINDS)
    if get_jobs_settings().test_jobs_enabled:
        kinds.add("noop.test")
    return kinds


router = APIRouter(
    prefix="/api/jobs",
    tags=["jobs"],
    responses={
        400: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
)


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class CreateJobRequest(BaseModel):
    kind: str
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("kind")
    @classmethod
    def kind_nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("kind must be a non-empty string")
        return v


# ---------------------------------------------------------------------------
# POST /api/jobs
# ---------------------------------------------------------------------------


@router.post("", status_code=201, response_model=JobCreateResponse)
@limiter.limit("30/minute")
async def create_job(
    request: Request,
    body: CreateJobRequest,
    user_id: int | None = Depends(current_user_id),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> JobCreateResponse:
    """Enqueue a new background job and return its ID."""
    public_kinds = _get_public_job_kinds()
    if body.kind not in public_kinds:
        raise HTTPException(
            status_code=400,
            detail=f"Job kind {body.kind!r} is not allowed. "
            f"Permitted kinds: {sorted(public_kinds)}",
        )
    job_id = await jobs_lib.enqueue(
        db_pool,
        body.kind,
        body.payload,
        user_id=str(user_id) if user_id is not None else None,
    )
    return JobCreateResponse(job_id=str(job_id), status="queued")


# ---------------------------------------------------------------------------
# GET /api/jobs/{job_id}
# ---------------------------------------------------------------------------


@router.get("/{job_id}", response_model=JobStatusResponse)
@limiter.limit("120/minute")
async def get_job(
    request: Request,
    job_id: str,
    user_id: int | None = Depends(current_user_id),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> dict[str, Any]:
    """Return the full job row for the given job_id."""
    row = await jobs_lib.get(db_pool, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")
    if row.get("user_id") is not None and str(row["user_id"]) != str(user_id):
        raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")
    return _serialise_row(row)


# ---------------------------------------------------------------------------
# GET /api/jobs
# ---------------------------------------------------------------------------


@router.get("", response_model=list[JobStatusResponse])
@limiter.limit("60/minute")
async def list_jobs(
    request: Request,
    status: str | None = Query(default=None),
    kind: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    user_id: int | None = Depends(current_user_id),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> list[dict[str, Any]]:
    """Return a list of jobs, optionally filtered by status and/or kind."""
    rows = await jobs_lib.list_jobs(
        db_pool,
        status=status,
        kind=kind,
        limit=limit,
        user_id=str(user_id) if user_id is not None else None,
    )
    return [_serialise_row(r) for r in rows]


# ---------------------------------------------------------------------------
# GET /api/jobs/{job_id}/stream  — SSE
# ---------------------------------------------------------------------------


@router.get("/{job_id}/stream")
@limiter.limit("10/minute")
async def stream_job(
    request: Request,
    job_id: str,
    user_id: int | None = Depends(current_user_id),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> StreamingResponse:
    """SSE stream of progress updates for the given job.

    Emits a ``data:`` event whenever progress, progress_message, or status
    changes.  Closes automatically on terminal status (succeeded/failed/cancelled).
    """
    pool = db_pool

    # Verify the job exists first and enforce ownership.
    # Use 404 (not 403) to avoid leaking job existence to unauthorized callers.
    initial = await jobs_lib.get(pool, job_id)
    if initial is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")
    if initial.get("user_id") is not None and str(initial["user_id"]) != str(user_id):
        raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")

    return StreamingResponse(
        jobs_lib.stream_job_events(pool, job_id, is_disconnected=request.is_disconnected),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# POST /api/jobs/{job_id}/cancel
# ---------------------------------------------------------------------------


@router.post("/{job_id}/cancel")
@limiter.limit("30/minute")
async def cancel_job(
    request: Request,
    job_id: str,
    user_id: int | None = Depends(current_user_id),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> dict[str, Any]:
    """Request cancellation of a running or queued job."""
    row = await jobs_lib.get(db_pool, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")
    if row.get("user_id") is not None and str(row["user_id"]) != str(user_id):
        raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")
    await jobs_lib.request_cancel(db_pool, job_id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _serialise_row(row: dict[str, Any]) -> dict[str, Any]:
    """Convert asyncpg row values (UUIDs, datetimes) to JSON-safe types."""
    out: dict[str, Any] = {}
    for k, v in row.items():
        if hasattr(v, "isoformat"):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out
