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
    """Serialised representation of a row from the ``jobs`` table.

    ``result`` / ``payload`` / ``error`` are opaque JSON blobs; timestamps
    are ISO-8601 strings (converted by :func:`_serialise_row`).
    """

    id: str
    kind: str
    status: str
    progress: int | None = None
    progress_message: str | None = None
    payload: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    user_id: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None

    model_config = {"extra": "allow"}


class JobListResponse(BaseModel):
    """Wrapper around a paginated list of jobs.

    The legacy endpoint returns a bare ``list[JobStatusResponse]``; this
    response model is provided for callers that want a documented envelope.
    """

    jobs: list[JobStatusResponse]
    total: int
