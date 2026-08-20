"""ASGI middleware for X-Request-ID propagation, plus shared response-header writes."""

import uuid
from collections.abc import Mapping

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from jarvis_common.logging_config import request_id_ctx


def set_response_headers(message: Message, values: Mapping[str, str]) -> None:
    """Set headers on an ``http.response.start`` message, replacing duplicates.

    ``headers`` is optional on that message, and ``MutableHeaders(scope=...)``
    raises ``KeyError`` when it is absent — an inner application that omits the
    field would otherwise turn a successful response into an unhandled error.
    Seeding the key first also preserves the in-place write-through the wrapper
    relies on, so no caller has to reassign ``message["headers"]``.

    Parameters
    ----------
    message : Message
        The ASGI ``http.response.start`` message to write into.
    values : Mapping[str, str]
        Header names mapped to their values.
    """
    message.setdefault("headers", [])
    headers = MutableHeaders(scope=message)
    for name, value in values.items():
        headers[name] = value


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
        # Starlette's outermost error handler runs after this middleware has
        # unwound and reset the context variable, so the scope state is the only
        # source of the id left for an unhandled 500. It is written directly
        # rather than through ``request.state`` because this middleware is pure
        # ASGI and also serves websocket scopes, where building a Request is
        # wrong.
        scope.setdefault("state", {})["request_id"] = rid

        async def send_with_rid(message: Message) -> None:
            if message["type"] == "http.response.start":
                set_response_headers(message, {"x-request-id": rid})
            await send(message)

        try:
            await self.app(scope, receive, send_with_rid)
        finally:
            request_id_ctx.reset(token)
