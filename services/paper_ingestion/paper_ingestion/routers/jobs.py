"""Jobs REST endpoints for paper_ingestion service.

Provides enqueue, status, list, SSE stream, and cancel endpoints.
The matching worker is wired into the service lifespan in main.py.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

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
from pydantic import BaseModel, Field, TypeAdapter, model_validator

from paper_ingestion.deps import get_db_pool, limiter

# ---------------------------------------------------------------------------
# Allowlist of job kinds that clients may create via POST /api/jobs.
# Internal-only kinds (paper.download, papers.scan_local, extraction.single,
# citations.batch_fetch, digest.weekly, paper.summarize) are deliberately
# excluded — they are only triggered by the service itself.
# ---------------------------------------------------------------------------
_BASE_PUBLIC_JOB_KINDS: frozenset[str] = frozenset(
    {
        "pulse.generate",
        "paper.process",
        "paper.analyze",
        "papers.batch_process",
        "papers.batch_summarize",
        "extraction.batch",
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


# ---------------------------------------------------------------------------
# Per-kind payload schemas — one model per public job kind.
#
# Wire format (unchanged from before this PR):
#   {"kind": "paper.process", "payload": {"paper_id": 42}}
#
# Pydantic's model_validator merges kind into the payload dict and parses it
# through a discriminated union, so unknown kinds and missing / wrong-typed
# required fields are rejected with 422 before create_job is even called.
# ---------------------------------------------------------------------------


class PulseGeneratePayload(BaseModel):
    kind: Literal["pulse.generate"]
    # Optional ISO timestamp used for deterministic testing only.
    now: str | None = None


class PaperProcessPayload(BaseModel):
    kind: Literal["paper.process"]
    paper_id: int
    force: bool = False


class PaperAnalyzePayload(BaseModel):
    kind: Literal["paper.analyze"]
    paper_id: int


class PapersBatchProcessPayload(BaseModel):
    kind: Literal["papers.batch_process"]
    paper_ids: list[int]


class PapersBatchSummarizePayload(BaseModel):
    kind: Literal["papers.batch_summarize"]
    paper_ids: list[int]


class ExtractionBatchPayload(BaseModel):
    kind: Literal["extraction.batch"]
    paper_ids: list[int]


class NoopTestPayload(BaseModel):
    """Test-only handler — only accepted when JARVIS_ENABLE_TEST_JOBS=1."""

    kind: Literal["noop.test"]
    # Allow any extra keys so test callers can attach markers without schema changes.
    model_config = {"extra": "allow"}


# Discriminated union over all public kinds.  Unknown kinds produce a clear
# 422 validation error via Pydantic's discriminator mechanism.
_JobPayload = Annotated[
    PulseGeneratePayload
    | PaperProcessPayload
    | PaperAnalyzePayload
    | PapersBatchProcessPayload
    | PapersBatchSummarizePayload
    | ExtractionBatchPayload
    | NoopTestPayload,
    Field(discriminator="kind"),
]


_JOB_PAYLOAD_ADAPTER: TypeAdapter[_JobPayload] | None = None


def _get_payload_adapter() -> TypeAdapter[_JobPayload]:
    """Lazily create the TypeAdapter (module-level _JobPayload must be fully defined first)."""
    global _JOB_PAYLOAD_ADAPTER  # noqa: PLW0603
    if _JOB_PAYLOAD_ADAPTER is None:
        _JOB_PAYLOAD_ADAPTER = TypeAdapter(_JobPayload)
    return _JOB_PAYLOAD_ADAPTER


class CreateJobRequest(BaseModel):
    """Validated job-creation request.

    Preserves the existing wire format ``{"kind": "...", "payload": {...}}``.
    The model_validator merges ``kind`` into the payload dict and parses it
    through ``_JobPayload``; Pydantic rejects unknown kinds and missing /
    wrong-typed required fields with 422 before ``create_job`` is called.
    """

    kind: str
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_payload_for_kind(self) -> CreateJobRequest:
        merged: dict[str, Any] = {**self.payload, "kind": self.kind}
        # Raises pydantic.ValidationError (→ HTTP 422) for unknown kinds or
        # missing / wrong-typed required fields.
        _get_payload_adapter().validate_python(merged)
        return self


# ---------------------------------------------------------------------------
# POST /api/jobs
# ---------------------------------------------------------------------------


@router.post("", status_code=201, response_model=JobCreateResponse)
@limiter.limit("30/minute")
async def create_job(
    request: Request,
    body: CreateJobRequest,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int | None = Depends(current_user_id),
) -> JobCreateResponse:
    """Enqueue a new background job and return its ID."""
    # Pydantic's discriminator already rejected unknown kinds with 422 before
    # we get here.  The runtime allowlist check guards noop.test in production.
    public_kinds = _get_public_job_kinds()
    if body.kind not in public_kinds:
        raise HTTPException(
            status_code=422,
            detail=f"Job kind {body.kind!r} is not allowed. "
            f"Permitted kinds: {sorted(public_kinds)}",
        )
    # TODO(Phase-2 multi-tenant): replace single-user assumption with
    # paper-ownership join when papers.user_id lands.
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
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int | None = Depends(current_user_id),
) -> dict[str, Any]:
    """Return the full job row for the given job_id."""
    row = await jobs_lib.get(db_pool, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")
    if row.get("user_id") is not None and row["user_id"] != user_id:
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
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int | None = Depends(current_user_id),
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
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int | None = Depends(current_user_id),
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
    if initial.get("user_id") is not None and initial["user_id"] != user_id:
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
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int | None = Depends(current_user_id),
) -> dict[str, Any]:
    """Request cancellation of a running or queued job."""
    row = await jobs_lib.get(db_pool, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")
    if row.get("user_id") is not None and row["user_id"] != user_id:
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
