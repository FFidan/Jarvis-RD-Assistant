"""Starlette middleware that attaches X-Correlation-Id to request scope."""

import uuid
from time import perf_counter

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from .logging_config import correlation_id_var
from .telemetry import record_request, request_span, trace_headers, trace_id


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Starlette middleware that propagates a per-request correlation ID.

    Accepts an inbound ``X-Correlation-Id`` UUID header when supplied;
    generates a fresh UUID otherwise (or when the header value is malformed).
    The ID is stored in :data:`jarvis_common.logging_config.correlation_id_var`
    for the duration of the request and echoed back in the response header.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        """Attach a correlation ID to the request context and response header."""
        header = request.headers.get("X-Correlation-Id")
        try:
            corr = uuid.UUID(header) if header else uuid.uuid4()
        except (ValueError, TypeError):
            corr = uuid.uuid4()
        token = correlation_id_var.set(corr)
        started = perf_counter()
        # ServerErrorMiddleware invokes a registered generic handler after this
        # middleware unwinds. Keep the identifiers on the request so that
        # handler can preserve response observability without leaking context.
        request.state.correlation_id = str(corr)
        request.state.telemetry_started = started
        try:
            with request_span(
                headers=request.headers,
                service=getattr(request.app.state, "service_name", "unknown"),
                method=request.method,
            ):
                request.state.trace_id = trace_id()
                response = await call_next(request)
                route = getattr(request.scope.get("route"), "path", "unmatched")
                record_request(
                    service=getattr(request.app.state, "service_name", "unknown"),
                    status_code=response.status_code,
                    duration_s=perf_counter() - started,
                    route=route,
                )
                response.headers.update(trace_headers())
        finally:
            correlation_id_var.reset(token)
        response.headers["X-Correlation-Id"] = str(corr)
        return response
