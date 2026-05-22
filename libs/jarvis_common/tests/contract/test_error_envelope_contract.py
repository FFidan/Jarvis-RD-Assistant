"""Shared error-envelope contract suite (audit X-07).

Asserts the canonical 4xx/5xx JSON envelope shape emitted by
``jarvis_common/error_handlers.py`` across services.  Parametrized over
(status_code, trigger_type) pairs.

Canonical error envelope (from ``error_handlers.py``, read at HEAD)
--------------------------------------------------------------------
All three handlers return:
  ``{"detail": <str>, "request_id": <str | null>}``

Specifically:
* ``http_exception_handler`` (StarletteHTTPException): ``{"detail": exc.detail, "request_id": ...}``
* ``validation_exception_handler`` (RequestValidationError 422): ``{"detail": "Validation error", "request_id": ...}``
* ``generic_exception_handler`` (Exception, 500): ``{"detail": "An internal error occurred.", "request_id": ...}``

Both keys are always present.  ``request_id`` is ``None`` when no
``X-Request-ID`` is in scope (which is the case in ASGI unit tests
without ``RequestIDMiddleware``).

Design
------
Rather than importing the real service apps (which require full DB
lifespan wiring), we build minimal FastAPI apps with the same
``configure_middleware_and_errors`` call (or direct handler registration)
so the contract is tested in isolation from service-specific init state.

The ``contract_conn`` / ``contract_two_users`` fixtures are NOT used here
because the tested paths need no DB: we only exercise the error-handler
layer, not the route logic.

Per-service error-shape assertions scattered across test files collapse
in Sub-wave 4.4 with this suite as the survivor citation.
"""

from __future__ import annotations

import pytest

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]

# ---------------------------------------------------------------------------
# Minimal app factory
# ---------------------------------------------------------------------------


def _make_error_test_app():
    """Build a minimal FastAPI app with the shared error handlers registered.

    Routes:
    * GET /ok                        → 200 {"ok": True}
    * GET /raise-http/{code}         → raises HTTPException(status_code=code)
    * POST /raise-validation         → 422 via Pydantic body validation failure
    * GET /raise-unhandled           → raises RuntimeError (→ 500)
    """
    from fastapi import FastAPI, HTTPException
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse
    from jarvis_common.error_handlers import (
        generic_exception_handler,
        http_exception_handler,
        validation_exception_handler,
    )
    from pydantic import BaseModel
    from starlette.exceptions import HTTPException as StarletteHTTPException

    app = FastAPI()
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, validation_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, generic_exception_handler)

    @app.get("/ok")
    async def ok() -> JSONResponse:  # noqa: RUF029
        return JSONResponse({"ok": True})

    @app.get("/raise-http/{code}")
    async def raise_http(code: int) -> JSONResponse:  # noqa: RUF029
        raise HTTPException(status_code=code, detail=f"forced {code}")

    class _Body(BaseModel):
        required_field: int  # missing or wrong type → 422

    @app.post("/raise-validation")
    async def raise_validation(body: _Body) -> JSONResponse:  # noqa: RUF029
        return JSONResponse({"value": body.required_field})

    @app.get("/raise-unhandled")
    async def raise_unhandled() -> JSONResponse:  # noqa: RUF029
        raise RuntimeError("deliberate unhandled error for contract test")

    return app


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

HTTP_ERROR_CASES = [
    (404, "GET", "/raise-http/404"),
    (403, "GET", "/raise-http/403"),
    (400, "GET", "/raise-http/400"),
    (503, "GET", "/raise-http/503"),
]


@pytest.mark.parametrize("expected_status,method,path", HTTP_ERROR_CASES)
async def test_http_exception_envelope_shape(
    expected_status: int,
    method: str,
    path: str,
) -> None:
    """HTTPException responses carry both ``detail`` and ``request_id`` keys.

    Envelope: ``{"detail": <str>, "request_id": <str | null>}``
    (``error_handlers.py:38-42``)
    """
    import httpx

    app = _make_error_test_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        resp = await client.request(method, path)

    assert resp.status_code == expected_status
    body = resp.json()
    assert "detail" in body, f"Missing 'detail' in {body}"
    assert "request_id" in body, f"Missing 'request_id' in {body}"
    assert isinstance(body["detail"], str) and body["detail"]
    # request_id is None when RequestIDMiddleware is not in the stack
    assert body["request_id"] is None or isinstance(body["request_id"], str)


async def test_validation_error_envelope_shape() -> None:
    """422 Pydantic validation errors carry the canonical envelope.

    Body shape (``error_handlers.py:55-76``):
    ``{"detail": "Validation error", "request_id": <null>}``
    (production mode — ``DEV_ERROR_DETAIL`` defaults to false).
    """
    import httpx

    app = _make_error_test_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        # Send wrong type for required_field (str instead of int) → 422
        resp = await client.post("/raise-validation", json={"required_field": "not-an-int"})

    assert resp.status_code == 422
    body = resp.json()
    assert "detail" in body, f"Missing 'detail' in {body}"
    assert "request_id" in body, f"Missing 'request_id' in {body}"
    assert body["detail"] == "Validation error"


async def test_validation_error_missing_body() -> None:
    """422 on missing required body also uses the canonical envelope."""
    import httpx

    app = _make_error_test_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        resp = await client.post("/raise-validation", json={})  # required_field absent

    assert resp.status_code == 422
    body = resp.json()
    assert "detail" in body
    assert "request_id" in body


async def test_unhandled_exception_envelope_shape() -> None:
    """Unhandled Python exceptions return 500 with the generic envelope.

    Body shape (``error_handlers.py:98-103``):
    ``{"detail": "An internal error occurred.", "request_id": <null>}``

    ``raise_app_exceptions=False`` suppresses ASGI transport's default
    re-raise so we can read the response that generic_exception_handler
    returns (confirmed emitted by the server-side log in the failing path).
    """
    import httpx

    app = _make_error_test_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        resp = await client.get("/raise-unhandled")

    assert resp.status_code == 500
    body = resp.json()
    assert "detail" in body, f"Missing 'detail' in {body}"
    assert "request_id" in body, f"Missing 'request_id' in {body}"
    assert body["detail"] == "An internal error occurred."


async def test_envelope_no_extra_keys_on_http_exception() -> None:
    """HTTPException envelope has EXACTLY ``detail`` and ``request_id`` — no extras.

    Guards against accidental schema drift (e.g. adding ``errors`` to the
    non-validation handler or leaking internal state).
    """
    import httpx

    app = _make_error_test_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        resp = await client.get("/raise-http/404")

    assert resp.status_code == 404
    assert set(resp.json().keys()) == {"detail", "request_id"}


async def test_envelope_no_extra_keys_on_unhandled_exception() -> None:
    """500 generic envelope also has EXACTLY ``detail`` and ``request_id``."""
    import httpx

    app = _make_error_test_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        resp = await client.get("/raise-unhandled")

    assert resp.status_code == 500
    assert set(resp.json().keys()) == {"detail", "request_id"}


async def test_ok_route_not_wrapped_in_envelope() -> None:
    """Sanity: successful responses do NOT get the error envelope injected."""
    import httpx

    app = _make_error_test_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        resp = await client.get("/ok")

    assert resp.status_code == 200
    body = resp.json()
    assert body == {"ok": True}
    assert "detail" not in body
    assert "request_id" not in body
