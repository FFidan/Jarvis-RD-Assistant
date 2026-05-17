"""Tests for the shared health-check skeleton (DOM-J-03).

Covers :mod:`jarvis_common.health`:
* :func:`run_health_checks` aggregator semantics (status mapping, exception
  resilience, ``"unknown"`` does not degrade).
* :func:`register_health_routes` produces a public ``GET /health`` (status
  only) and an authenticated ``GET /health/internal`` (full envelope).
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport
from jarvis_common import verify_api_key
from jarvis_common.health import register_health_routes, run_health_checks

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request() -> Request:
    """A minimal Request stand-in.  Probes here ignore it but accept the arg."""
    # An asgi scope is needed for fastapi.Request init; an empty placeholder works.
    return Request({"type": "http", "headers": []})


def _make_app(checks: list[tuple[str, object]], service_name: str = "test") -> FastAPI:
    app = FastAPI()
    # Bypass auth on /health/internal so tests don't have to thread a real key.
    app.dependency_overrides[verify_api_key] = lambda: None
    register_health_routes(app, service_name=service_name, checks=checks)  # type: ignore[arg-type]
    return app


# ---------------------------------------------------------------------------
# run_health_checks aggregator
# ---------------------------------------------------------------------------


class TestRunHealthChecks:
    @pytest.mark.asyncio
    async def test_aggregates_probes_in_order(self) -> None:
        """Probes run in declared order and their results land under the right keys."""

        async def probe_a(_r: Request) -> str:
            return "ok"

        async def probe_b(_r: Request) -> str:
            return "ok"

        status, results = await run_health_checks(
            _make_request(),
            [("a", probe_a), ("b", probe_b)],
        )

        assert status == "ok"
        assert results == {"a": "ok", "b": "ok"}
        # dict insertion order matches declared order
        assert list(results.keys()) == ["a", "b"]

    @pytest.mark.asyncio
    async def test_degraded_when_any_probe_returns_unavailable(self) -> None:
        async def probe_ok(_r: Request) -> str:
            return "ok"

        async def probe_bad(_r: Request) -> str:
            return "unavailable"

        status, results = await run_health_checks(
            _make_request(),
            [("a", probe_ok), ("b", probe_bad)],
        )

        assert status == "degraded"
        assert results == {"a": "ok", "b": "unavailable"}

    @pytest.mark.asyncio
    async def test_unknown_does_not_degrade(self) -> None:
        """``"unknown"`` is treated as non-degraded (intentionally disabled deps)."""

        async def probe_ok(_r: Request) -> str:
            return "ok"

        async def probe_unknown(_r: Request) -> str:
            return "unknown"

        status, results = await run_health_checks(
            _make_request(),
            [("a", probe_ok), ("b", probe_unknown)],
        )

        assert status == "ok"
        assert results == {"a": "ok", "b": "unknown"}

    @pytest.mark.asyncio
    async def test_probe_exception_maps_to_unavailable(self) -> None:
        """A probe that raises is recorded as ``unavailable`` (best-effort)."""

        async def probe_explodes(_r: Request) -> str:
            raise RuntimeError("boom")

        status, results = await run_health_checks(
            _make_request(),
            [("a", probe_explodes)],
        )

        assert status == "degraded"
        assert results == {"a": "unavailable"}

    @pytest.mark.asyncio
    async def test_slow_probe_recorded_as_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """L-08: a hung probe must not stall ``/health`` — it returns ``"timeout"``.

        Patch the module-level probe timeout to a tiny value so the test runs
        in milliseconds.
        """
        import jarvis_common.health as health_module

        monkeypatch.setattr(health_module, "_PROBE_TIMEOUT_S", 0.05)

        async def probe_hung(_r: Request) -> str:
            await asyncio.sleep(10.0)
            return "ok"

        async def probe_fast(_r: Request) -> str:
            return "ok"

        status, results = await run_health_checks(
            _make_request(),
            [("fast", probe_fast), ("hung", probe_hung)],
        )

        assert results["fast"] == "ok"
        assert results["hung"] == "timeout"
        assert status == "degraded"


# ---------------------------------------------------------------------------
# register_health_routes — HTTP-level tests
# ---------------------------------------------------------------------------


class TestRegisterHealthRoutes:
    @pytest.mark.asyncio
    async def test_register_health_routes_aggregates_probes(self) -> None:
        """``GET /health`` returns 200 + status='ok' and ``/health/internal``
        returns the full ``{status, service, checks}`` envelope when every
        probe is healthy."""

        async def probe_a(_r: Request) -> str:
            return "ok"

        async def probe_b(_r: Request) -> str:
            return "ok"

        app = _make_app([("a", probe_a), ("b", probe_b)], service_name="dummy")

        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/health")
            assert resp.status_code == 200
            body = resp.json()
            assert body == {"status": "ok"}
            # Public endpoint must NOT expose dependency details (SEC-H09)
            assert "checks" not in body
            assert "service" not in body

            resp_internal = await client.get("/health/internal")
            assert resp_internal.status_code == 200
            envelope = resp_internal.json()
            assert envelope["status"] == "ok"
            assert envelope["service"] == "dummy"
            assert envelope["checks"] == {"a": "ok", "b": "ok"}

    @pytest.mark.asyncio
    async def test_health_endpoint_returns_degraded_on_probe_failure(self) -> None:
        """Public ``/health`` returns 503 with status='degraded' and **no
        checks dict** when any probe fails.  ``/health/internal`` returns
        503 *with* the checks dict so operators can see the failure."""

        async def probe_ok(_r: Request) -> str:
            return "ok"

        async def probe_fail(_r: Request) -> str:
            return "unavailable"

        app = _make_app(
            [("postgres", probe_ok), ("litellm", probe_fail)],
            service_name="dummy",
        )

        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/health")
            assert resp.status_code == 503
            body = resp.json()
            assert body["status"] == "degraded"
            assert "checks" not in body

            resp_internal = await client.get("/health/internal")
            assert resp_internal.status_code == 503
            envelope = resp_internal.json()
            assert envelope["status"] == "degraded"
            assert envelope["service"] == "dummy"
            assert envelope["checks"] == {"postgres": "ok", "litellm": "unavailable"}

    @pytest.mark.asyncio
    async def test_health_internal_requires_auth(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``/health/internal`` rejects unauthenticated callers, public ``/health`` does not.

        Use a real API-key configuration so the dependency is enforced.
        """
        monkeypatch.setenv("JARVIS_API_KEY", "test-key-secret")
        # Refresh the cached API key so the new value takes effect.
        from jarvis_common.auth import refresh_api_key_cache

        refresh_api_key_cache()

        async def probe_ok(_r: Request) -> str:
            return "ok"

        # Build app without the verify_api_key override so the real dep runs.
        app = FastAPI()
        register_health_routes(app, service_name="dummy", checks=[("a", probe_ok)])

        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            # Public endpoint is open
            resp_public = await client.get("/health")
            assert resp_public.status_code == 200

            # Internal endpoint without key → 401/403 (verify_api_key raises)
            resp_internal = await client.get("/health/internal")
            assert resp_internal.status_code in (401, 403)

            # With key → 200 + full envelope
            resp_ok = await client.get("/health/internal", headers={"X-API-Key": "test-key-secret"})
            assert resp_ok.status_code == 200
            assert resp_ok.json()["checks"] == {"a": "ok"}


class TestHealthLiveAndExemption:
    @pytest.mark.asyncio
    async def test_health_live_returns_ok_without_probes(self) -> None:
        """``/health/live`` is a pure liveness signal: 200 + status='ok' even
        when every readiness probe is failing (it never runs them)."""

        async def probe_fail(_r: Request) -> str:
            return "unavailable"

        app = _make_app([("postgres", probe_fail)], service_name="dummy")

        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            live = await client.get("/health/live")
            assert live.status_code == 200
            assert live.json() == {"status": "ok"}

            # /health (readiness) still degrades on the same failing probe.
            ready = await client.get("/health")
            assert ready.status_code == 503

    @pytest.mark.asyncio
    async def test_health_routes_exempt_from_global_rate_limit(self) -> None:
        """When a limiter is supplied, the global ``default_limits`` cap never
        throttles the health routes — a saturated shared IP bucket must not
        starve a monitoring/LB poll (the 429-under-load defect)."""
        from jarvis_common.http_rate_limiter import (
            create_limiter,
            rate_limit_exceeded_handler,
        )
        from slowapi.errors import RateLimitExceeded
        from slowapi.middleware import SlowAPIMiddleware

        async def probe_ok(_r: Request) -> str:
            return "ok"

        limiter = create_limiter(default_limits=["1/minute"])
        app = FastAPI()
        app.dependency_overrides[verify_api_key] = lambda: None
        app.state.limiter = limiter
        app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)
        app.add_middleware(SlowAPIMiddleware)

        @app.get("/control")
        async def control() -> dict[str, str]:
            return {"ok": "yes"}

        register_health_routes(app, service_name="dummy", checks=[("a", probe_ok)], limiter=limiter)

        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            # The 1/minute global cap is real: a non-exempt route is throttled.
            assert (await client.get("/control")).status_code == 200
            assert (await client.get("/control")).status_code == 429

            # Health routes stay 200 across many calls despite the exhausted bucket.
            for _ in range(5):
                assert (await client.get("/health/live")).status_code == 200
                assert (await client.get("/health")).status_code == 200
                assert (await client.get("/health/internal")).status_code == 200


# ---------------------------------------------------------------------------
# Shared probe factories (L-10)
# ---------------------------------------------------------------------------


class TestMakePostgresProbe:
    @pytest.mark.asyncio
    async def test_returns_ok_on_success(self) -> None:
        """A pool whose SELECT 1 succeeds yields ``"ok"``."""
        from unittest.mock import AsyncMock

        from jarvis_common.health import make_postgres_probe

        conn = AsyncMock()
        conn.fetchval = AsyncMock(return_value=1)
        pool = MagicMock()
        pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
        pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        probe = make_postgres_probe(pool)
        result = await probe(_make_request())
        assert result == "ok"
        conn.fetchval.assert_awaited_once_with("SELECT 1")

    @pytest.mark.asyncio
    async def test_returns_unavailable_on_failure(self) -> None:
        """Any exception from the pool surfaces as ``"unavailable"``."""
        from unittest.mock import AsyncMock

        from jarvis_common.health import make_postgres_probe

        pool = MagicMock()
        pool.acquire.return_value.__aenter__ = AsyncMock(side_effect=RuntimeError("pg down"))
        pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        probe = make_postgres_probe(pool)
        result = await probe(_make_request())
        assert result == "unavailable"

    @pytest.mark.asyncio
    async def test_deferred_pool_resolved_from_request_state(self) -> None:
        """When pool is omitted, the probe reads it from ``request.app.state.db_pool``."""
        from unittest.mock import AsyncMock

        from fastapi import FastAPI
        from jarvis_common.health import make_postgres_probe

        conn = AsyncMock()
        conn.fetchval = AsyncMock(return_value=1)
        pool = MagicMock()
        pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
        pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        app = FastAPI()
        app.state.db_pool = pool

        probe = make_postgres_probe()  # deferred

        async def _call() -> str:
            scope = {"type": "http", "headers": [], "app": app}
            return await probe(Request(scope))

        result = await _call()
        assert result == "ok"
