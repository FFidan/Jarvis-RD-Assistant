"""Tests for jarvis_common.correlation_middleware.CorrelationIdMiddleware."""

from __future__ import annotations

import uuid

from jarvis_common.correlation_middleware import CorrelationIdMiddleware
from jarvis_common.logging_config import correlation_id_var
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

# ---------------------------------------------------------------------------
# Minimal Starlette app for testing
# ---------------------------------------------------------------------------


async def _echo_endpoint(request: Request) -> PlainTextResponse:
    """Return the current correlation_id as the response body."""
    current = correlation_id_var.get()
    return PlainTextResponse(str(current) if current else "none")


def _make_app() -> Starlette:
    app = Starlette(routes=[Route("/echo", _echo_endpoint)])
    app.add_middleware(CorrelationIdMiddleware)
    return app


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_correlation_middleware_propagates_existing_header():
    """When the client sends a valid UUID in X-Correlation-Id, the middleware must use it."""
    app = _make_app()
    client = TestClient(app, raise_server_exceptions=True)

    known_id = str(uuid.uuid4())
    response = client.get("/echo", headers={"X-Correlation-Id": known_id})

    assert response.status_code == 200
    # The correlation ID set in the contextvar must match the one we sent
    assert response.text == known_id


def test_correlation_middleware_generates_uuid_when_missing():
    """When X-Correlation-Id is absent, the middleware must generate a fresh UUID."""
    app = _make_app()
    client = TestClient(app, raise_server_exceptions=True)

    response = client.get("/echo")

    assert response.status_code == 200
    # Response body is the correlation_id_var value — must be a valid UUID
    generated = response.text
    assert generated != "none", "Expected a generated UUID, got 'none'"
    parsed = uuid.UUID(generated)  # raises ValueError if not a valid UUID
    assert parsed.version == 4


def test_correlation_middleware_returns_id_in_response_header():
    """The middleware must echo X-Correlation-Id in the response headers."""
    app = _make_app()
    client = TestClient(app, raise_server_exceptions=True)

    known_id = str(uuid.uuid4())
    response = client.get("/echo", headers={"X-Correlation-Id": known_id})

    assert response.status_code == 200
    assert response.headers.get("x-correlation-id") == known_id


def test_correlation_middleware_generates_response_header_when_absent():
    """Even when no header is sent, the response must include X-Correlation-Id."""
    app = _make_app()
    client = TestClient(app, raise_server_exceptions=True)

    response = client.get("/echo")

    assert response.status_code == 200
    header_val = response.headers.get("x-correlation-id")
    assert header_val is not None
    uuid.UUID(header_val)  # raises ValueError if not a valid UUID


def test_correlation_middleware_ignores_malformed_header():
    """A non-UUID X-Correlation-Id must be replaced with a fresh UUID."""
    app = _make_app()
    client = TestClient(app, raise_server_exceptions=True)

    response = client.get("/echo", headers={"X-Correlation-Id": "not-a-uuid"})

    assert response.status_code == 200
    generated_id = response.headers.get("x-correlation-id")
    assert generated_id != "not-a-uuid"
    uuid.UUID(generated_id)  # must be a valid UUID
