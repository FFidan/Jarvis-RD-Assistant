"""Safe classification of job/bulk-action failures into caller-facing codes.

Deliberately free of any FastAPI import: this module is shared by the API
router layer (``routers/papers_bulk.py``) and the background job/worker
layer (``paper_jobs.py``), and the worker layer must not depend on the web
framework. ``HTTPException`` instances are recognized structurally, via
their ``status_code`` attribute, rather than by ``isinstance``.
"""

from __future__ import annotations

import asyncpg


def classify_bulk_error(exc: Exception) -> str:
    """Map an exception to a safe, operator-diagnostic response code.

    Raw exception messages (asyncpg constraint names, SQL text, HTTP detail
    strings) are never forwarded to the caller -- only the returned code is
    meant to cross a job/API boundary.

    Parameters
    ----------
    exc : Exception
        The exception raised while handling one paper, job, or bulk action.

    Returns
    -------
    str
        One of ``"not_found"``, ``"forbidden"``, ``"conflict"``,
        ``"http_error"``, ``"already_in_state"``, ``"constraint_error"``,
        ``"db_error"``, ``"invalid_action"``, or ``"unknown_error"``.
    """
    status_code = getattr(exc, "status_code", None)
    if type(exc).__name__ == "HTTPException" and isinstance(status_code, int):
        if status_code == 404:
            return "not_found"
        if status_code == 403:
            return "forbidden"
        if status_code == 409:
            return "conflict"
        return "http_error"
    if isinstance(exc, asyncpg.UniqueViolationError):
        return "already_in_state"
    if isinstance(exc, asyncpg.ForeignKeyViolationError):
        return "not_found"
    if isinstance(exc, asyncpg.NotNullViolationError | asyncpg.CheckViolationError):
        return "constraint_error"
    if isinstance(exc, asyncpg.PostgresError):
        return "db_error"
    if isinstance(exc, ValueError):
        return "invalid_action"
    return "unknown_error"
