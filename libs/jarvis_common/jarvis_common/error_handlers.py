"""Shared exception handlers for FastAPI services."""

import logging

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from jarvis_common.logging_config import request_id_ctx
from jarvis_common.settings import get_core_settings

logger = logging.getLogger(__name__)


def _is_dev_mode() -> bool:
    return get_core_settings().dev_error_detail


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """Return a standardised JSON error body for Starlette ``HTTPException``.

    The response includes the exception ``detail`` string and the current
    ``X-Request-ID`` (``None`` when no request ID is in scope).

    Parameters
    ----------
    request:
        The incoming FastAPI/Starlette request (used for context only).
    exc:
        The ``HTTPException`` instance raised by route or middleware code.

    Returns
    -------
    JSONResponse
        ``{"detail": ..., "request_id": ...}`` with the original status code.
    """
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
    """Catch-all handler for unhandled exceptions — always returns HTTP 500.

    Logs the full traceback server-side (keyed by ``X-Request-ID``) and
    returns a generic ``"An internal error occurred."`` response so exception
    details are never leaked to clients.

    Parameters
    ----------
    request:
        The incoming FastAPI/Starlette request.
    exc:
        The unhandled exception.

    Returns
    -------
    JSONResponse
        HTTP 500 with ``{"detail": "An internal error occurred.", "request_id": ...}``.
    """
    request_id = request_id_ctx.get("") or None
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": "An internal error occurred.", "request_id": request_id},
    )
