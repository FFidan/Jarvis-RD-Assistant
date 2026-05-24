"""ASGI middleware for X-Request-ID propagation."""

import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from jarvis_common.logging_config import request_id_ctx

# Maximum length of a client-supplied X-Request-ID. UUIDv4 is 36 chars; we
# allow a generous 128 to fit alternative correlation schemes (Cloudflare ray
# IDs, OpenTelemetry trace IDs, …) without letting an attacker bloat the
# response header.
_MAX_REQUEST_ID_LEN = 128

# Header-injection guard: stripping CR / LF / NUL prevents response splitting
# when the client X-Request-ID is echoed verbatim into the response headers.
_FORBIDDEN_HEADER_CHARS = ("\r", "\n", "\x00")


def _sanitise_request_id(raw: str) -> str:
    """Strip header-injection characters and clamp to ``_MAX_REQUEST_ID_LEN``.

    Returns ``""`` when the input becomes empty after sanitisation; the
    middleware then generates a fresh UUIDv4 instead.
    """
    cleaned = raw
    for ch in _FORBIDDEN_HEADER_CHARS:
        cleaned = cleaned.replace(ch, "")
    if len(cleaned) > _MAX_REQUEST_ID_LEN:
        cleaned = cleaned[:_MAX_REQUEST_ID_LEN]
    return cleaned


class RequestIDMiddleware:
    """Read or generate X-Request-ID, store in contextvars, set response header.

    A client-supplied ``X-Request-ID`` is truncated to 128 chars and stripped
    of CR / LF / NUL bytes before being echoed into the response — an
    attacker cannot use it to inject extra response headers (HTTP response
    splitting) or smuggle a multi-megabyte log line into structured logs.

    Parameters
    ----------
    app : ASGIApp
        The wrapped ASGI application.

    """

    def __init__(self, app: ASGIApp) -> None:
        """Wrap *app* to add X-Request-ID propagation on every HTTP/WebSocket scope."""
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Extract or generate a request ID, set it in context, and echo it in the response."""
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        raw = (headers.get(b"x-request-id", b"") or b"").decode(errors="replace")
        rid = _sanitise_request_id(raw) or str(uuid.uuid4())
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
