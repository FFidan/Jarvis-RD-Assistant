"""ASGI middleware for X-Request-ID propagation."""

import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from jarvis_common.logging_config import request_id_ctx


class RequestIDMiddleware:
    """Read or generate X-Request-ID, store in contextvars, set response header.

    Parameters
    ----------
    app : ASGIApp
        The wrapped ASGI application.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        rid = (headers.get(b"x-request-id", b"") or b"").decode() or str(uuid.uuid4())
        token = request_id_ctx.set(rid)

        async def send_with_rid(message: Message) -> None:
            if message["type"] == "http.response.start":
                resp_headers = list(message.get("headers", []))
                resp_headers.append((b"x-request-id", rid.encode()))
                message["headers"] = resp_headers
            await send(message)

        try:
            await self.app(scope, receive, send_with_rid)
        finally:
            request_id_ctx.reset(token)
