"""Tests for bridging unhandled 500s into the Events log (Task 6.1).

``generic_exception_handler`` is the FastAPI catch-all for unhandled
exceptions. Before this change it only logged (via the stdlib ``logging``
module) and returned a generic 500 — the admin-facing Events tab
(``category="error"``) stayed near-empty since almost nothing wrote to it.
This adds a best-effort, deduped, rate-limited write to ``system_events`` via
``log_event`` so operators see unhandled 500s without grepping service logs.
The write can never affect the response: it is wrapped in ``try/except`` and
skipped entirely when no DB pool is available.

Verified identifiers:
- libs/jarvis_common/jarvis_common/error_handlers.py:80 — generic_exception_handler
- libs/jarvis_common/jarvis_common/error_handlers.py — _should_emit_error_event,
  _ERROR_EVENT_WINDOW_SECONDS, _last_error_event_emitted
- libs/jarvis_common/jarvis_common/event_log.py:14 — log_event(*, pool, level,
  category, source, message, context=None)
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jarvis_common import error_handlers
from jarvis_common.telemetry import configure_telemetry


def _make_request(
    *, path: str = "/api/papers", method: str = "GET", pool: object | None
) -> MagicMock:
    """Build a minimal mock Request with ``request.app.state.db_pool`` set."""
    request = MagicMock()
    request.method = method
    request.url = SimpleNamespace(path=path)
    request.app = SimpleNamespace(state=SimpleNamespace(db_pool=pool))
    return request


@pytest.fixture(autouse=True)
def _reset_dedup_map():
    """Each test starts with a clean dedup map — it's module-level state."""
    error_handlers._last_error_event_emitted.clear()
    yield
    error_handlers._last_error_event_emitted.clear()


@pytest.mark.asyncio
async def test_unhandled_exception_writes_one_error_event() -> None:
    """A single unhandled exception writes exactly one category="error" event."""
    mock_pool = MagicMock()
    request = _make_request(pool=mock_pool)
    exc = ValueError("boom")

    with patch.object(error_handlers, "log_event", new_callable=AsyncMock) as mock_log_event:
        response = await error_handlers.generic_exception_handler(request, exc)

    assert response.status_code == 500
    mock_log_event.assert_awaited_once()
    call_kwargs = mock_log_event.call_args.kwargs
    assert call_kwargs["category"] == "error"
    assert call_kwargs["context"]["route"] == "/api/papers"


@pytest.mark.asyncio
async def test_burst_of_same_exception_and_route_dedupes_to_one_event() -> None:
    """Three back-to-back identical (exc_type, route) failures write ONE event."""
    mock_pool = MagicMock()
    request = _make_request(pool=mock_pool)

    with patch.object(error_handlers, "log_event", new_callable=AsyncMock) as mock_log_event:
        for _ in range(3):
            await error_handlers.generic_exception_handler(request, ValueError("boom"))

    mock_log_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_different_exception_type_is_not_deduped() -> None:
    """A different exception type on the same route is NOT deduped."""
    mock_pool = MagicMock()
    request = _make_request(pool=mock_pool)

    with patch.object(error_handlers, "log_event", new_callable=AsyncMock) as mock_log_event:
        await error_handlers.generic_exception_handler(request, ValueError("boom"))
        await error_handlers.generic_exception_handler(request, KeyError("boom"))

    assert mock_log_event.await_count == 2


@pytest.mark.asyncio
async def test_different_route_is_not_deduped() -> None:
    """The same exception type on a different route is NOT deduped."""
    mock_pool = MagicMock()
    request_a = _make_request(path="/api/papers", pool=mock_pool)
    request_b = _make_request(path="/api/settings", pool=mock_pool)

    with patch.object(error_handlers, "log_event", new_callable=AsyncMock) as mock_log_event:
        await error_handlers.generic_exception_handler(request_a, ValueError("boom"))
        await error_handlers.generic_exception_handler(request_b, ValueError("boom"))

    assert mock_log_event.await_count == 2


@pytest.mark.asyncio
async def test_no_pool_skips_event_but_still_returns_500() -> None:
    """No DB pool on app.state → handler still returns 500 and never raises."""
    request = _make_request(pool=None)

    with patch.object(error_handlers, "log_event", new_callable=AsyncMock) as mock_log_event:
        response = await error_handlers.generic_exception_handler(request, ValueError("boom"))

    assert response.status_code == 500
    mock_log_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_response_body_unchanged() -> None:
    """The 500 JSON body is unchanged by the new event-logging side effect."""
    mock_pool = MagicMock()
    request = _make_request(pool=mock_pool)

    with patch.object(error_handlers, "log_event", new_callable=AsyncMock):
        response = await error_handlers.generic_exception_handler(request, ValueError("boom"))

    assert response.status_code == 500
    assert json.loads(response.body) == {
        "detail": "An internal error occurred.",
        "request_id": None,
        "correlation_id": None,
        "trace_id": None,
    }


@pytest.mark.asyncio
async def test_generic_error_response_keeps_trace_evidence_and_records_one_red_outcome() -> None:
    """The registered 500 response preserves identifiers and records exactly one RED result."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from jarvis_common.correlation_middleware import CorrelationIdMiddleware

    configure_telemetry(service="test", enabled=False, otlp_endpoint=None, timeout_ms=1)
    app = FastAPI()
    app.add_middleware(CorrelationIdMiddleware)
    app.add_exception_handler(Exception, error_handlers.generic_exception_handler)

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("failure")

    with (
        patch.object(error_handlers, "record_request") as record_request,
        patch.object(error_handlers, "log_event", new_callable=AsyncMock),
    ):
        response = TestClient(app, raise_server_exceptions=False).get("/boom")

    assert response.status_code == 500
    assert response.headers["x-correlation-id"] == response.json()["correlation_id"]
    assert response.headers["x-trace-id"] == response.json()["trace_id"]
    record_request.assert_called_once()


@pytest.mark.asyncio
async def test_generic_error_response_keeps_the_request_id_of_the_failed_request() -> None:
    """An unhandled 500 reports the same request id the client sent.

    ``RequestIDMiddleware`` is pure ASGI and resets its context variable while
    unwinding, which happens before the outermost error handler builds this
    response. Reading that variable alone therefore reports no id at all, and
    the operator cannot join the 500 body to the request in the logs.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from jarvis_common.correlation_middleware import CorrelationIdMiddleware
    from jarvis_common.request_id import RequestIDMiddleware

    configure_telemetry(service="test", enabled=False, otlp_endpoint=None, timeout_ms=1)
    app = FastAPI()
    # Same order as configure_middleware_and_errors: request id innermost.
    app.add_middleware(RequestIDMiddleware)
    app.add_middleware(CorrelationIdMiddleware)
    app.add_exception_handler(Exception, error_handlers.generic_exception_handler)

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("failure")

    with (
        patch.object(error_handlers, "record_request"),
        patch.object(error_handlers, "log_event", new_callable=AsyncMock),
    ):
        response = TestClient(app, raise_server_exceptions=False).get(
            "/boom", headers={"X-Request-ID": "client-supplied-id"}
        )

    assert response.status_code == 500
    assert response.json()["request_id"] == "client-supplied-id"
