"""Tests for jarvis_common.request_id.RequestIDMiddleware sanitisation."""

from __future__ import annotations

import pytest
from jarvis_common.request_id import (
    _MAX_REQUEST_ID_LEN,
    RequestIDMiddleware,
    _sanitise_request_id,
)


class _DummyApp:
    """Minimal ASGI app that captures the response-start headers."""

    def __init__(self) -> None:
        self.captured_headers: list[tuple[bytes, bytes]] | None = None

    async def __call__(self, scope, receive, send):  # type: ignore[no-untyped-def]
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


async def _drive(middleware: RequestIDMiddleware, headers: dict[bytes, bytes]) -> str:
    """Drive the middleware once and return the X-Request-ID it set."""
    captured: dict[str, str] = {}

    async def receive():  # type: ignore[no-untyped-def]
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):  # type: ignore[no-untyped-def]
        if message["type"] == "http.response.start":
            for k, v in message.get("headers", []):
                if k.lower() == b"x-request-id":
                    captured["rid"] = v.decode("latin-1")

    scope = {
        "type": "http",
        "headers": list(headers.items()),
    }
    await middleware(scope, receive, send)
    return captured.get("rid", "")


def test_sanitise_strips_crlf_and_null():
    raw = "abc\r\ndef\x00ghi"
    assert _sanitise_request_id(raw) == "abcdefghi"


def test_sanitise_truncates_to_max_len():
    raw = "x" * (_MAX_REQUEST_ID_LEN + 50)
    out = _sanitise_request_id(raw)
    assert len(out) == _MAX_REQUEST_ID_LEN
    assert out == "x" * _MAX_REQUEST_ID_LEN


def test_sanitise_empty_after_strip_returns_empty():
    """All-CRLF input collapses to empty (caller must generate a UUID)."""
    assert _sanitise_request_id("\r\n\r\n") == ""


@pytest.mark.asyncio
async def test_middleware_truncates_oversize_client_id():
    app = _DummyApp()
    mw = RequestIDMiddleware(app)
    huge = ("a" * 5000).encode()
    rid = await _drive(mw, {b"x-request-id": huge})
    assert len(rid) <= _MAX_REQUEST_ID_LEN
    assert rid == "a" * _MAX_REQUEST_ID_LEN


@pytest.mark.asyncio
async def test_middleware_strips_crlf_in_client_id():
    """A client X-Request-ID with CRLF must NOT propagate raw to response."""
    app = _DummyApp()
    mw = RequestIDMiddleware(app)
    rid = await _drive(mw, {b"x-request-id": b"abc\r\nX-Injected: evil"})
    assert "\r" not in rid
    assert "\n" not in rid
    assert rid.startswith("abc")


@pytest.mark.asyncio
async def test_middleware_generates_uuid_when_input_only_crlf():
    """If the client header is entirely junk, fall back to a fresh UUID."""
    app = _DummyApp()
    mw = RequestIDMiddleware(app)
    rid = await _drive(mw, {b"x-request-id": b"\r\n\r\n"})
    # UUIDv4 is 36 chars (8-4-4-4-12 with 4 dashes).
    assert len(rid) == 36
    assert rid.count("-") == 4


@pytest.mark.asyncio
async def test_middleware_preserves_well_formed_id():
    app = _DummyApp()
    mw = RequestIDMiddleware(app)
    well_formed = b"5b8aa5a2-d2e2-4b86-9c5c-1234567890ab"
    rid = await _drive(mw, {b"x-request-id": well_formed})
    assert rid == well_formed.decode()
