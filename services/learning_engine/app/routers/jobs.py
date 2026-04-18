"""Jobs REST endpoints for learning_engine service.

Provides enqueue, status, list, SSE stream, and cancel endpoints.
The matching worker is wired into the service lifespan in main.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from jarvis_common import current_user_id, verify_api_key
from jarvis_common import jobs as jobs_lib
from pydantic import BaseModel, field_validator

from app.deps import limiter

logger = logging.getLogger(__name__)

# SSE keepalive / max-stream constants
_KEEPALIVE_INTERVAL = 15.0  # seconds between keepalive comments
_MAX_STREAM_SECONDS = 750  # hard ceiling; yields streaming_timeout and exits

# ---------------------------------------------------------------------------
# Allowlist of job kinds that clients may create via POST /api/jobs.
# card.generate_batch is excluded from the public API — it is a large batch
# operation that should only be triggered through the /api/generation/batch
# endpoint with its own validation.
# ---------------------------------------------------------------------------
_PUBLIC_JOB_KINDS: set[str] = {
    "card.generate",
}
if os.getenv("DEV_MODE", "false").lower() == "true":
    _PUBLIC_JOB_KINDS.add("noop.test")

router = APIRouter(
    prefix="/api/jobs",
    tags=["jobs"],
    dependencies=[Depends(verify_api_key)],
)


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class CreateJobRequest(BaseModel):
    kind: str
    payload: dict[str, Any] = {}
    user_id: str | None = None

    @field_validator("kind")
    @classmethod
    def kind_nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("kind must be a non-empty string")
        return v


# ---------------------------------------------------------------------------
# POST /api/jobs
# ---------------------------------------------------------------------------


@router.post("", status_code=201)
@limiter.limit("30/minute")
async def create_job(request: Request, body: CreateJobRequest) -> dict[str, Any]:
    """Enqueue a new background job and return its ID."""
    if body.kind not in _PUBLIC_JOB_KINDS:
        raise HTTPException(
            status_code=422,
            detail=f"Job kind {body.kind!r} is not allowed. "
            f"Permitted kinds: {sorted(_PUBLIC_JOB_KINDS)}",
        )
    job_id = await jobs_lib.enqueue(
        request.app.state.db_pool,
        body.kind,
        body.payload,
        user_id=body.user_id,
    )
    return {"job_id": job_id, "status": "queued"}


# ---------------------------------------------------------------------------
# GET /api/jobs/{job_id}
# ---------------------------------------------------------------------------


@router.get("/{job_id}")
@limiter.limit("120/minute")
async def get_job(request: Request, job_id: str) -> dict[str, Any]:
    """Return the full job row for the given job_id."""
    row = await jobs_lib.get(request.app.state.db_pool, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")
    return _serialise_row(row)


# ---------------------------------------------------------------------------
# GET /api/jobs
# ---------------------------------------------------------------------------


@router.get("")
@limiter.limit("60/minute")
async def list_jobs(
    request: Request,
    status: str | None = Query(default=None),
    kind: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
) -> list[dict[str, Any]]:
    """Return a list of jobs, optionally filtered by status and/or kind."""
    rows = await jobs_lib.list_jobs(
        request.app.state.db_pool,
        status=status,
        kind=kind,
        limit=limit,
    )
    return [_serialise_row(r) for r in rows]


# ---------------------------------------------------------------------------
# GET /api/jobs/{job_id}/stream  — SSE
# ---------------------------------------------------------------------------


@router.get("/{job_id}/stream")
async def stream_job(
    request: Request,
    job_id: str,
    user_id: int | None = Depends(current_user_id),
) -> StreamingResponse:
    """SSE stream of progress updates for the given job.

    Emits a ``data:`` event whenever progress, progress_message, or status
    changes.  Closes automatically on terminal status (succeeded/failed/cancelled).
    """
    pool = request.app.state.db_pool

    # Verify the job exists first and enforce ownership.
    # Use 404 (not 403) to avoid leaking job existence to unauthorized callers.
    initial = await jobs_lib.get(pool, job_id)
    if initial is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")
    if initial.get("user_id") is not None and initial["user_id"] != user_id:
        raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")

    _terminal_statuses = frozenset({"succeeded", "failed", "cancelled"})

    async def _event_generator():
        last_key: tuple | None = None
        loop = asyncio.get_event_loop()
        loop_start = loop.time()
        last_keepalive = loop_start

        while True:
            if await request.is_disconnected():
                logger.debug("SSE client disconnected for job %s", job_id)
                break

            now = loop.time()
            elapsed = now - loop_start

            # Hard ceiling — prevent zombie streams
            if elapsed > _MAX_STREAM_SECONDS:
                logger.warning("SSE stream timeout for job %s after %.0fs", job_id, elapsed)
                yield f"data: {json.dumps({'status': 'streaming_timeout'})}\n\n"
                break

            # Keepalive comment to prevent proxy / browser from closing idle connection
            if now - last_keepalive >= _KEEPALIVE_INTERVAL:
                yield ": keepalive\n\n"
                last_keepalive = now

            row = await jobs_lib.get(pool, job_id)
            if row is None:
                break

            key = (row.get("progress"), row.get("progress_message"), row["status"])
            if key != last_key:
                last_key = key
                event_data: dict[str, Any] = {
                    "progress": row.get("progress"),
                    "progress_message": row.get("progress_message"),
                    "status": row["status"],
                }
                if row["status"] in _terminal_statuses:
                    if row.get("result") is not None:
                        event_data["result"] = row["result"]
                    if row.get("error") is not None:
                        event_data["error"] = row["error"]
                yield f"data: {json.dumps(event_data)}\n\n"

            if row["status"] in _terminal_statuses:
                break

            await asyncio.sleep(0.5)

    return StreamingResponse(
        _event_generator(),
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
async def cancel_job(request: Request, job_id: str) -> dict[str, Any]:
    """Request cancellation of a running or queued job."""
    row = await jobs_lib.get(request.app.state.db_pool, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")
    await jobs_lib.request_cancel(request.app.state.db_pool, job_id)
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
