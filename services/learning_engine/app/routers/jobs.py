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
_BASE_PUBLIC_JOB_KINDS: frozenset[str] = frozenset(
    {
        "card.generate",
    }
)


def _get_public_job_kinds() -> set[str]:
    """Return the set of allowed job kinds, evaluated at request time.

    DEV_MODE is read on each call so that it can be toggled without a restart
    (e.g. in integration tests that set os.environ["DEV_MODE"] at runtime).
    """
    kinds = set(_BASE_PUBLIC_JOB_KINDS)
    if os.getenv("DEV_MODE", "false").lower() == "true":
        kinds.add("noop.test")
    return kinds


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
async def create_job(
    request: Request,
    body: CreateJobRequest,
    user_id: int | None = Depends(current_user_id),
) -> dict[str, Any]:
    """Enqueue a new background job and return its ID."""
    public_kinds = _get_public_job_kinds()
    if body.kind not in public_kinds:
        raise HTTPException(
            status_code=422,
            detail=f"Job kind {body.kind!r} is not allowed. "
            f"Permitted kinds: {sorted(public_kinds)}",
        )
    job_id = await jobs_lib.enqueue(
        request.app.state.db_pool,
        body.kind,
        body.payload,
        user_id=str(user_id) if user_id is not None else None,
    )
    return {"job_id": job_id, "status": "queued"}


# ---------------------------------------------------------------------------
# GET /api/jobs/{job_id}
# ---------------------------------------------------------------------------


@router.get("/{job_id}")
@limiter.limit("120/minute")
async def get_job(
    request: Request,
    job_id: str,
    user_id: int | None = Depends(current_user_id),
) -> dict[str, Any]:
    """Return the full job row for the given job_id."""
    row = await jobs_lib.get(request.app.state.db_pool, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")
    if row.get("user_id") is not None and row["user_id"] != user_id:
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
    user_id: int | None = Depends(current_user_id),
) -> list[dict[str, Any]]:
    """Return a list of jobs, optionally filtered by status and/or kind."""
    rows = await jobs_lib.list_jobs(
        request.app.state.db_pool,
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
        loop = asyncio.get_running_loop()
        loop_start = loop.time()
        last_keepalive = loop_start

        poll_interval = 2.0
        idle_ticks = 0
        last_state: tuple | None = None

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

            # Adaptive poll backoff: reset to 2s on any row change; ramp up to 5s
            # after 30s of no changes to reduce unnecessary DB load.
            current_state = (row.get("progress"), row.get("progress_message"), row["status"])
            if current_state != last_state:
                last_state = current_state
                idle_ticks = 0
                poll_interval = 2.0
            else:
                idle_ticks += 1
                if idle_ticks * poll_interval > 30:
                    poll_interval = min(poll_interval + 1.0, 5.0)

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
                    if row.get("payload") is not None:
                        event_data["payload"] = row["payload"]
                yield f"data: {json.dumps(event_data)}\n\n"

            if row["status"] in _terminal_statuses:
                break

            await asyncio.sleep(poll_interval)

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
async def cancel_job(
    request: Request,
    job_id: str,
    user_id: int | None = Depends(current_user_id),
) -> dict[str, Any]:
    """Request cancellation of a running or queued job."""
    row = await jobs_lib.get(request.app.state.db_pool, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")
    if row.get("user_id") is not None and row["user_id"] != user_id:
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
