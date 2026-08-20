"""Shared exception handlers for FastAPI services."""

import logging
import time

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from jarvis_common.event_log import log_event
from jarvis_common.logging_config import request_id_ctx
from jarvis_common.settings import get_core_settings
from jarvis_common.telemetry import correlation_id, record_request, trace_id

logger = logging.getLogger(__name__)

# Bridges unhandled 500s into the `system_events` Events log so operators see
# them without grepping service logs. Best-effort, deduped, and bounded — see
# generic_exception_handler().
_ERROR_EVENT_SOURCE = "api"
_ERROR_EVENT_WINDOW_SECONDS = 60.0
_ERROR_EVENT_DEDUP_MAX = 512

# Keyed by (exception type name, route path) -> monotonic time of last emit.
_last_error_event_emitted: dict[tuple[str, str], float] = {}


def _stashed_identifier(request: Request, name: str) -> str | None:
    """Return a string identifier the middleware stack left on the ASGI scope state."""
    value = getattr(request.state, name, None)
    return value if isinstance(value, str) else None


def _request_identifiers(request: Request) -> tuple[str | None, str | None, str | None]:
    """Return the correlation, trace and request identifiers for this request.

    Each middleware that owns one of these resets its context variable while
    unwinding, which happens before Starlette's outermost handler runs. The
    values stashed on the scope state are therefore the only ones that survive
    that far, and the active context is preferred only while it still holds one.
    """
    return (
        correlation_id() or _stashed_identifier(request, "correlation_id"),
        trace_id() or _stashed_identifier(request, "trace_id"),
        request_id_ctx.get("") or _stashed_identifier(request, "request_id"),
    )


def _trace_headers(correlation: str | None, trace: str | None) -> dict[str, str]:
    """Return bounded request identifiers for a response sent by exception middleware."""
    values = {
        "X-Correlation-Id": correlation,
        "X-Trace-Id": trace,
    }
    return {name: value for name, value in values.items() if value is not None}


def _record_unhandled_request(request: Request) -> None:
    """Record the RED result that bypassed correlation middleware on exception."""
    route = getattr(request.scope.get("route"), "path", "unmatched")
    service = getattr(request.app.state, "service_name", "unknown")
    started = getattr(request.state, "telemetry_started", None)
    duration_s = time.perf_counter() - started if isinstance(started, (int, float)) else 0.0
    record_request(
        service=service if isinstance(service, str) else "unknown",
        status_code=500,
        duration_s=max(duration_s, 0.0),
        route=route if isinstance(route, str) else "unmatched",
    )


def _should_emit_error_event(key: tuple[str, str], now: float) -> bool:
    """Return True (and record ``now``) iff ``key`` hasn't fired within the window.

    Bounds ``_last_error_event_emitted`` so a 500-storm across many distinct
    routes/exception types can't grow it without limit: once over
    ``_ERROR_EVENT_DEDUP_MAX`` entries, stale (out-of-window) entries are
    evicted first, falling back to a full clear if that isn't enough.
    """
    last = _last_error_event_emitted.get(key)
    if last is not None and now - last < _ERROR_EVENT_WINDOW_SECONDS:
        return False
    if len(_last_error_event_emitted) > _ERROR_EVENT_DEDUP_MAX:
        stale_keys = [
            k
            for k, ts in _last_error_event_emitted.items()
            if now - ts >= _ERROR_EVENT_WINDOW_SECONDS
        ]
        for k in stale_keys:
            del _last_error_event_emitted[k]
        if len(_last_error_event_emitted) > _ERROR_EVENT_DEDUP_MAX:
            _last_error_event_emitted.clear()
    _last_error_event_emitted[key] = now
    return True


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
        content={
            "detail": exc.detail,
            "request_id": request_id,
            "correlation_id": correlation_id(),
            "trace_id": trace_id(),
        },
    )


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Handle Pydantic/FastAPI validation errors.

    In production (DEV_MODE=false) the response body is redacted to
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
                "correlation_id": correlation_id(),
                "trace_id": trace_id(),
            },
        )
    # Production: log full details server-side, return a generic message.
    logger.warning(
        "Validation error [request_id=%s]: %s",
        request_id,
        [{k: v for k, v in e.items() if k != "input"} for e in exc.errors()],
    )
    return JSONResponse(
        status_code=422,
        content={
            "detail": "Validation error",
            "request_id": request_id,
            "correlation_id": correlation_id(),
            "trace_id": trace_id(),
        },
    )


async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all handler for unhandled exceptions — always returns HTTP 500.

    Logs the full traceback server-side (keyed by ``X-Request-ID``) and
    returns a generic ``"An internal error occurred."`` response so exception
    details are never leaked to clients. Also makes a best-effort, deduped
    attempt to record the failure as a ``category="error"`` row in the Events
    log (see :func:`_should_emit_error_event`) so operators see unhandled 500s
    without grepping service logs; this can never affect the response.

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
    current_correlation_id, current_trace_id, request_id = _request_identifiers(request)
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    try:
        _record_unhandled_request(request)
        pool = getattr(request.app.state, "db_pool", None)
        if pool is not None:
            key = (type(exc).__name__, request.url.path)
            if _should_emit_error_event(key, time.monotonic()):
                await log_event(
                    pool=pool,
                    level="error",
                    category="error",
                    source=_ERROR_EVENT_SOURCE,
                    message=type(exc).__name__,
                    context={"route": request.url.path, "method": request.method},
                )
    except Exception:
        # Last line of defense: an unhandled-exception handler must never
        # itself raise, no matter what goes wrong recording the event.
        pass
    return JSONResponse(
        status_code=500,
        content={
            "detail": "An internal error occurred.",
            "request_id": request_id,
            "correlation_id": current_correlation_id,
            "trace_id": current_trace_id,
        },
        headers=_trace_headers(current_correlation_id, current_trace_id),
    )
