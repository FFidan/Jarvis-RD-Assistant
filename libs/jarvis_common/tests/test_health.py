"""Tests for the shared health-check skeleton (DOM-J-03).

Covers :mod:`jarvis_common.health`:
* :func:`run_health_checks` aggregator semantics (status mapping, exception
  resilience, ``"unknown"`` does not degrade).
* :func:`register_health_routes` produces a public ``GET /health`` (status
  only) and an authenticated ``GET /health/internal`` (full envelope).
"""

from __future__ import annotations

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


# ---------------------------------------------------------------------------
# warn_multitenant_stub (DOM-J-02)
# ---------------------------------------------------------------------------


class TestWarnMultitenantStub:
    @pytest.mark.asyncio
    async def test_no_warn_when_multitenant_disabled(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.delenv("MULTITENANT_ENABLED", raising=False)
        from jarvis_common.app_factory import warn_multitenant_stub

        caplog.set_level("CRITICAL")
        await warn_multitenant_stub(MagicMock())
        critical_records = [r for r in caplog.records if r.levelname == "CRITICAL"]
        assert critical_records == []

    @pytest.mark.asyncio
    async def test_warns_critical_when_multitenant_enabled(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("MULTITENANT_ENABLED", "true")
        from jarvis_common.app_factory import warn_multitenant_stub

        caplog.set_level("CRITICAL")
        await warn_multitenant_stub(MagicMock())
        critical_records = [r for r in caplog.records if r.levelname == "CRITICAL"]
        assert critical_records, "expected a CRITICAL log line when MULTITENANT_ENABLED=true"
        assert "stub" in critical_records[0].getMessage().lower()
