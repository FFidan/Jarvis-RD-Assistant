"""Starlette middleware that attaches X-Correlation-Id to request scope."""

import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from .logging_config import correlation_id_var


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        header = request.headers.get("X-Correlation-Id")
        try:
            corr = uuid.UUID(header) if header else uuid.uuid4()
        except (ValueError, TypeError):
            corr = uuid.uuid4()
        token = correlation_id_var.set(corr)
        try:
            response = await call_next(request)
        finally:
            correlation_id_var.reset(token)
        response.headers["X-Correlation-Id"] = str(corr)
        return response
