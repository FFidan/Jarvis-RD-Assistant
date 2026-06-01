"""Shared Pydantic models for JARVIS microservices."""

from typing import Any

from pydantic import BaseModel


class HealthCheckResponse(BaseModel):
    """Standard health check response for all services."""

    status: str
    service: str
    checks: dict[str, str]


class ErrorResponse(BaseModel):
    """Standard error envelope emitted by the shared FastAPI exception handlers.

    Matches the JSON body produced by
    :func:`jarvis_common.error_handlers.http_exception_handler` /
    ``validation_exception_handler`` / ``generic_exception_handler``.
    """

    detail: str
    request_id: str | None = None


class JobCreateResponse(BaseModel):
    """Response body returned by ``POST /api/jobs``."""

    job_id: str
    status: str


class JobStatusResponse(BaseModel):
    """Serialised representation of a procrastinate-backed job.

    Rows are sourced from ``procrastinate_jobs`` (via
    :func:`jarvis_common.jobs.list_jobs` or
    :func:`jarvis_common.jobs.get_unified`) and normalised to this shape
    by :func:`jarvis_common.jobs.procrastinate_row_to_jarvis_row`.
    ``result`` / ``payload`` / ``error`` are opaque JSON blobs; timestamps
    are ISO-8601 strings.
    """

    id: str
    kind: str
    status: str
    progress: float | None = None
    progress_message: str | None = None
    payload: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    user_id: int | None = None
    created_at: str | None = None
    updated_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None

    model_config = {"extra": "allow"}
