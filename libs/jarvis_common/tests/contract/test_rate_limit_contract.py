"""Shared rate-limit contract suite (audit X-07).

Asserts the 429 response shape (status code + body) produced by
``jarvis_common.http_rate_limiter.rate_limit_exceeded_handler`` — the
canonical handler wired by ``configure_middleware_and_errors`` in both
``paper_ingestion`` and ``learning_engine``.

Design notes
------------
* The limiter instance in each service is a module-level singleton backed by
  in-memory storage (``limits.storage.MemoryStorage``).  Hitting the singleton
  in a parametrized test causes inter-test window pollution: a trip in one
  param variant exhausts the quota for the same key in subsequent variants.
  We therefore build a **fresh, isolated FastAPI app** per test that wires a
  fresh limiter + the shared handler, keeping the contract assertion clean
  without requiring per-service DB setup or real route logic.

* The 429 body shape is derived from
  ``jarvis_common/http_rate_limiter.py:137-143`` (read at HEAD):
  ``{"detail": "Rate limit exceeded (...). Please try again later."}``.
  No ``Retry-After`` header is emitted (the custom handler does not set it).

* The ``@pytest.mark.contract`` marker gates these under
  ``JARVIS_RUN_LIVE_PG=1`` via ``conftest.contract_pg_dsn``; however these
  tests need NO live DB — they run regardless.  The ``JARVIS_RUN_LIVE_PG``
  check is by-passed by not using the contract_pg_dsn fixture here.

The existing per-service rate-limit tests collapse in Sub-wave 4.4 to
"is the limiter wired" smokes citing this suite as survivor.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.contract, pytest.mark.asyncio(loop_scope="session")]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app(limit_string: str):
    """Build a minimal FastAPI app with a fresh limiter and one POST /ping."""
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse
    from jarvis_common.http_rate_limiter import create_limiter, rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded
    from slowapi.middleware import SlowAPIMiddleware

    limiter = create_limiter(default_limits=[], user_aware=False)

    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)  # type: ignore[arg-type]
    app.add_middleware(SlowAPIMiddleware)

    @app.post("/ping")
    @limiter.limit(limit_string)
    async def ping(request: Request) -> JSONResponse:  # noqa: RUF029
        return JSONResponse({"ok": True})

    return app


# ---------------------------------------------------------------------------
# Parametrized: limit strings to probe (trip_count = calls needed to exceed)
# ---------------------------------------------------------------------------

LIMIT_CASES = [
    (
        "3/minute",
        4,
    ),  # mirrors POST /api/pulse/generate ("3/hour") and /api/setup/admin ("3/minute")
    ("2/minute", 3),  # mirrors POST /api/citations/batch ("2/minute")
]


@pytest.mark.parametrize("limit_string,trip_count", LIMIT_CASES)
async def test_rate_limit_returns_429_with_canonical_body(
    limit_string: str,
    trip_count: int,
) -> None:
    """Hammering past the per-key limit yields 429 with the canonical body shape.

    The 429 body contract (from ``http_rate_limiter.py:137-143``):
    - status code: 429
    - body: ``{"detail": "<message>"}`` where message starts with
      ``"Rate limit exceeded"``
    - no ``Retry-After`` header (the custom handler does not emit it)
    """
    import httpx

    app = _make_app(limit_string)
    last_resp: httpx.Response | None = None
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        for _ in range(trip_count):
            last_resp = await client.post("/ping")

    assert last_resp is not None
    assert last_resp.status_code == 429
    body = last_resp.json()
    assert "detail" in body
    assert "Rate limit exceeded" in body["detail"]
    # Custom handler (http_rate_limiter.py:137-143) does NOT emit Retry-After.
    assert "retry-after" not in {k.lower() for k in last_resp.headers}


async def test_rate_limit_pre_trip_requests_succeed(limit_string: str = "3/minute") -> None:
    """Requests before the limit is exhausted return 2xx — limiter is not over-eager."""
    import httpx

    app = _make_app(limit_string)
    allowed_count = int(limit_string.split("/")[0])  # "3/minute" → 3
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        for _ in range(allowed_count):
            resp = await client.post("/ping")
            assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"


async def test_rate_limit_handler_body_has_no_extra_keys() -> None:
    """429 body contains ONLY ``detail`` — no ``request_id`` or other fields.

    The handler in http_rate_limiter.py is intentionally simpler than the
    general http_exception_handler (which adds request_id). Asserting this
    avoids accidental schema drift.
    """
    import httpx

    app = _make_app("1/minute")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        await client.post("/ping")  # first call: ok (within limit)
        resp = await client.post("/ping")  # second call: trips "1/minute"

    assert resp.status_code == 429
    body = resp.json()
    assert set(body.keys()) == {"detail"}, f"Unexpected body keys: {set(body.keys())}"
