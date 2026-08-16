"""Shared Pydantic models for JARVIS microservices."""

from typing import Any

from pydantic import BaseModel


class DatabaseRuntimeDiagnostics(BaseModel):
    """Read-only database role, schema, and pool details for operators."""

    current_user: str | None = None
    packaged_schema_version: int | None = None
    live_schema_version: int | None = None
    integrity: str | None = None
    migration_check_outcome: str | None = None
    migration_check_duration_ms: int | None = None
    pool_size: int | None = None
    pool_idle: int | None = None
    pool_max: int | None = None
    pool_wait_pressure: bool | None = None


class HealthDiagnostics(BaseModel):
    """Authenticated operational details that do not require observability."""

    correlation_id: str | None = None
    database: DatabaseRuntimeDiagnostics | None = None


class HealthCheckResponse(BaseModel):
    """Standard health check response for all services.

    ``maintenance`` and ``version`` are populated only by ``/health/internal``;
    ``/health`` and ``/health/live`` stay minimal.
    """

    status: str
    service: str
    checks: dict[str, str]
    maintenance: bool = False
    version: str | None = None
    diagnostics: HealthDiagnostics | None = None


class ErrorResponse(BaseModel):
    """Standard error envelope emitted by the shared FastAPI exception handlers.

    Matches the JSON body produced by
    :func:`jarvis_common.error_handlers.http_exception_handler` /
    ``validation_exception_handler`` / ``generic_exception_handler``.
    """

    detail: str
    request_id: str | None = None


class JobCreateResponse(BaseModel):
    """Response body returned by ``POST /api/jobs`` and job-enqueueing endpoints.

    ``job_id`` is null only when ``status == "skipped"`` — the endpoint decided
    not to queue a job and ``reason`` carries the machine-readable cause.
    Regular enqueues always return a string ``job_id`` and omit ``reason``.
    """

    job_id: str | None
    status: str
    reason: str | None = None


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
