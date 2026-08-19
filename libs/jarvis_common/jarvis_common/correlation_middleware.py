"""Starlette middleware that attaches X-Correlation-Id to request scope."""

import uuid
from time import perf_counter

from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .logging_config import correlation_id_var
from .telemetry import record_request, request_span, trace_headers, trace_id


class CorrelationIdMiddleware:
    """Starlette middleware that propagates a per-request correlation ID.

    Accepts an inbound ``X-Correlation-Id`` UUID header when supplied;
    generates a fresh UUID otherwise (or when the header value is malformed).
    The ID is stored in :data:`jarvis_common.logging_config.correlation_id_var`
    for the duration of the request and echoed back in the response header.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Wrap an ASGI application with correlation and request telemetry.

        Parameters
        ----------
        app : ASGIApp
            Application that receives the enriched request scope.
        """
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Attach correlation and trace context to one HTTP request.

        Parameters
        ----------
        scope : Scope
            ASGI connection scope.
        receive : Receive
            ASGI receive callable.
        send : Send
            ASGI send callable.
        """
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
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

        async def send_with_observability(message: Message) -> None:
            """Attach response identifiers and record the completed request."""
            if message["type"] == "http.response.start":
                route = getattr(scope.get("route"), "path", "unmatched")
                record_request(
                    service=getattr(request.app.state, "service_name", "unknown"),
                    status_code=int(message["status"]),
                    duration_s=perf_counter() - started,
                    route=route,
                )
                headers = MutableHeaders(scope=message)
                for name, value in trace_headers().items():
                    headers[name] = value
                headers["X-Correlation-Id"] = str(corr)
            await send(message)

        try:
            with request_span(
                headers=request.headers,
                service=getattr(request.app.state, "service_name", "unknown"),
                method=request.method,
            ):
                request.state.trace_id = trace_id()
                await self.app(scope, receive, send_with_observability)
        finally:
            correlation_id_var.reset(token)
