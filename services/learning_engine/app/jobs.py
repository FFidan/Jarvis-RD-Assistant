"""Simple in-memory job status registry for background tasks."""

from typing import Any

from jarvis_common.time_utils import utc_now_iso

_jobs: dict[str, dict[str, Any]] = {}


def create_job(job_id: str) -> dict[str, Any]:
    """Register a new job with *pending* status and return its initial record."""
    _jobs[job_id] = {
        "status": "pending",
        "created_at": utc_now_iso(),
        "result": None,
    }
    return _jobs[job_id]


def update_job(job_id: str, **kwargs: Any) -> None:
    """Merge *kwargs* into an existing job record (no-op if job is unknown)."""
    if job_id in _jobs:
        _jobs[job_id].update(kwargs)


def get_job(job_id: str) -> dict[str, Any] | None:
    """Return the job record for *job_id*, or ``None`` if not found."""
    return _jobs.get(job_id)
