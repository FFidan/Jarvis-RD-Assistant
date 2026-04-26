"""Shared exception handlers for FastAPI services."""

import logging
import os

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from jarvis_common.logging_config import request_id_ctx

logger = logging.getLogger(__name__)


def _is_dev_mode() -> bool:
    """Return True when DEV_MODE=true (case-insensitive)."""
    return os.environ.get("DEV_MODE", "false").lower() == "true"


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    request_id = request_id_ctx.get("") or None
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "request_id": request_id},
    )


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Handle Pydantic/FastAPI validation errors.

    SEC-107: In production (DEV_MODE=false) the response body is redacted to
    avoid leaking internal field names or input values.  Full pydantic error
    details are logged server-side, keyed by request_id for correlation.
    """
    request_id = request_id_ctx.get("") or None
    if _is_dev_mode():
        # Developer-friendly: surface field-level errors in the response.
        return JSONResponse(
            status_code=422,
            content={
                "detail": "Validation error",
                "errors": [
                    {k: v for k, v in e.items() if k not in ("input", "url")} for e in exc.errors()
                ],
                "request_id": request_id,
            },
        )
    # Production: log full details server-side, return a generic message.
    logger.warning(
        "Validation error [request_id=%s]: %s",
        request_id,
        exc.errors(),
    )
    return JSONResponse(
        status_code=422,
        content={"detail": "Validation error", "request_id": request_id},
    )


async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    request_id = request_id_ctx.get("") or None
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": "An internal error occurred.", "request_id": request_id},
    )
