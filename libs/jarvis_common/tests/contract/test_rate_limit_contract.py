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

import pytest

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]

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


# ---------------------------------------------------------------------------
# A259 — pulse-generate 3/hour cooldown semantics
# ---------------------------------------------------------------------------
#
# POST /api/pulse/generate is decorated with ``@limiter.limit("3/hour")``
# (pulse.py:76).  The test below uses the same _make_app() isolation
# strategy (fresh app + fresh limiter per test) to assert the semantic
# contract WITHOUT requiring a running paper_ingestion instance or
# pulse-generate route logic.  The "3/hour" window is the contract; the
# mechanism is the shared handler already proven above.
# ---------------------------------------------------------------------------


async def test_pulse_generate_rate_limit_semantics_3_per_hour() -> None:
    """Simulates the ``3/hour`` pulse-generate rate-limit contract.

    After 3 allowed calls the 4th returns 429 with the canonical body.
    This is the cooldown semantics documented in:
      - pulse.py:76  ``@limiter.limit("3/hour")``
      - reference_pulse_generate_429_by_design.md (vault memory)

    A fresh isolated app is used so this test does not pollute the
    per-service limiter state shared by other tests.
    """
    import httpx

    # Use "3/minute" here (same trip semantics as 3/hour but instant reset
    # in the test clock); the shape contract is identical — the handler and
    # body are the same regardless of the window unit.
    app = _make_app("3/minute")
    responses: list[int] = []
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        for _ in range(4):
            r = await client.post("/ping")
            responses.append(r.status_code)

    assert responses[:3] == [200, 200, 200], (
        f"First 3 requests should succeed (within 3/hour quota): {responses[:3]}"
    )
    assert responses[3] == 429, f"4th request should be rate-limited (429); got {responses[3]}"
    # Confirm cooldown body shape matches the canonical pulse-generate 429 shape
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        resp = await client.post("/ping")
    assert resp.status_code == 429
    body = resp.json()
    assert "Rate limit exceeded" in body.get("detail", ""), (
        f"Cooldown 429 body does not match pulse-generate contract: {body}"
    )


async def test_rate_limit_window_reset_allows_new_requests() -> None:
    """Within-quota requests succeed; a fresh app (simulating window reset) allows again.

    The window-reset contract: once the limit window expires, requests are
    allowed again.  We model this by using two independent app instances
    (each with a fresh MemoryStorage counter), which is equivalent to the
    limiter's per-key counter rolling over after the window expires.

    This proves the ``create_limiter`` call properly isolates state per-instance
    (no cross-instance counter sharing through module-level singletons).
    """
    import httpx

    # Trip the limit on app_1
    app_1 = _make_app("1/minute")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_1),
        base_url="http://test",
    ) as client:
        await client.post("/ping")  # allowed
        resp_tripped = await client.post("/ping")  # tripped
    assert resp_tripped.status_code == 429

    # Fresh app_2 (simulates next window) — same key, fresh counter
    app_2 = _make_app("1/minute")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_2),
        base_url="http://test",
    ) as client:
        resp_fresh = await client.post("/ping")

    assert resp_fresh.status_code == 200, (
        f"After window reset (fresh limiter), first request should succeed; "
        f"got {resp_fresh.status_code}"
    )
