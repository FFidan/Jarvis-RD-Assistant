"""Tests for CorrelationIdMiddleware and its registration in configure_middleware_and_errors.

DRY-C2 coverage:
- Middleware reads inbound X-Correlation-Id and echoes it back.
- Middleware generates a valid UUID when no header is supplied.
- configure_middleware_and_errors registers the middleware so a real FastAPI
  app emits the header on every response.
"""

from __future__ import annotations

import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
from jarvis_common.app_factory import configure_middleware_and_errors
from jarvis_common.correlation_middleware import CorrelationIdMiddleware
from slowapi import Limiter
from slowapi.util import get_remote_address

# ---------------------------------------------------------------------------
# Standalone middleware tests (no app_factory dependency)
# ---------------------------------------------------------------------------


def _make_minimal_app() -> FastAPI:
    """Minimal FastAPI app with only CorrelationIdMiddleware attached."""
    app = FastAPI()
    app.add_middleware(CorrelationIdMiddleware)

    @app.get("/ping")
    async def ping():
        return {"ok": True}

    return app


_VALID_UUID = "550e8400-e29b-41d4-a716-446655440000"


def test_correlation_middleware_echoes_supplied_header():
    """When X-Correlation-Id is a valid UUID it must be echoed in the response."""
    client = TestClient(_make_minimal_app(), raise_server_exceptions=True)
    resp = client.get("/ping", headers={"X-Correlation-Id": _VALID_UUID})
    assert resp.status_code == 200
    # Middleware stores the UUID object and stringifies it; normalise case for comparison.
    assert resp.headers["x-correlation-id"].lower() == _VALID_UUID.lower()


def test_correlation_middleware_generates_uuid_when_absent():
    """When X-Correlation-Id is absent, the middleware generates a valid UUID4."""
    client = TestClient(_make_minimal_app(), raise_server_exceptions=True)
    resp = client.get("/ping")
    assert resp.status_code == 200
    corr_id = resp.headers.get("x-correlation-id")
    assert corr_id is not None
    # Must be parseable as a UUID
    parsed = uuid.UUID(corr_id)
    assert parsed.version == 4


def test_correlation_middleware_replaces_malformed_header():
    """A malformed X-Correlation-Id is silently replaced with a fresh UUID4."""
    client = TestClient(_make_minimal_app(), raise_server_exceptions=True)
    resp = client.get("/ping", headers={"X-Correlation-Id": "not-a-uuid"})
    assert resp.status_code == 200
    corr_id = resp.headers.get("x-correlation-id")
    # Middleware generated a fresh UUID instead of echoing the malformed value
    assert corr_id != "not-a-uuid"
    uuid.UUID(corr_id)  # must be valid


# ---------------------------------------------------------------------------
# DRY-C2 integration: configure_middleware_and_errors registers the middleware
# ---------------------------------------------------------------------------


def test_correlation_middleware_registered_and_emits_header(monkeypatch):
    """configure_middleware_and_errors must register CorrelationIdMiddleware.

    Verifies that a FastAPI app built via the shared factory function emits
    X-Correlation-Id on every response, both when the header is supplied by
    the client and when it is absent.
    """
    # Patch env so configure_middleware_and_errors can resolve CORS settings
    monkeypatch.setenv("CORS_ORIGINS", "http://localhost:3001")
    monkeypatch.setenv("DEV_MODE", "false")
    monkeypatch.setenv("DEV_CORS_OPEN", "false")

    app = FastAPI()

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    limiter = Limiter(key_func=get_remote_address)
    configure_middleware_and_errors(app, limiter=limiter, cors_origins=["http://localhost:3001"])

    client = TestClient(app, raise_server_exceptions=True)

    # Case 1: supplied valid-UUID header is echoed (middleware parses+re-stringifies)
    resp = client.get("/health", headers={"X-Correlation-Id": _VALID_UUID})
    assert resp.status_code == 200
    assert resp.headers.get("x-correlation-id", "").lower() == _VALID_UUID.lower()

    # Case 2: no header → generated UUID4
    resp2 = client.get("/health")
    assert resp2.status_code == 200
    generated = resp2.headers.get("x-correlation-id")
    assert generated is not None
    uuid.UUID(generated)  # raises if not valid UUID
