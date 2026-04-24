"""Shared exception handlers for FastAPI services."""

import logging

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from jarvis_common.logging_config import request_id_ctx

logger = logging.getLogger(__name__)


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    request_id = request_id_ctx.get("") or None
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "request_id": request_id},
    )


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    request_id = request_id_ctx.get("") or None
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


async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    request_id = request_id_ctx.get("") or None
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": "An internal error occurred.", "request_id": request_id},
    )
