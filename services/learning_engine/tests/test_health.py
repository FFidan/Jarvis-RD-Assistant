"""Tests for GET /health and /health/internal endpoints of the learning_engine service.

Public /health (ζ4: SEC-H09):
- Returns only {"status": "ok"|"degraded"} — no dependency details exposed.
- HTTP 200 when all deps are reachable; HTTP 503 when any check fails.

Authenticated /health/internal:
- Returns full {status, service, checks} payload.
- Requires valid API key.

Also covers M26 regression: HealthCheckResponse importable from jarvis_common.
"""

from __future__ import annotations

import httpx
import pytest
from httpx import ASGITransport


# test_health_returns_200_when_ok deleted — covered by shared
# libs/jarvis_common/tests/contract/test_health_contract.py (X-02 audit).
# test_health_returns_503_when_degraded deleted — same survivor.
# test_health_internal_returns_full_details deleted — same survivor.
# test_health_internal_503_has_all_checks deleted — same survivor.
# The two tests below are LE-specific and kept.


def test_health_check_response_importable():
    """M26 regression: HealthCheckResponse must be exported from jarvis_common."""
    from jarvis_common import HealthCheckResponse

    assert HealthCheckResponse is not None


# ---------------------------------------------------------------------------
# Regression: HEALTH-LIVE-403 + SEC-AUTH-1
#
# The _HEALTH_PATHS exemption in verify_api_key must cover /health/live so
# that unauthenticated liveness probes are never blocked by the global
# app-level dependency. /health/internal must remain 403 without a key.
#
# These tests intentionally do NOT override verify_api_key — they exercise
# the real global dependency gate.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_live_accessible_without_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unauthenticated GET /health/live must return 200 — not 403.

    Exercises the real global verify_api_key dependency (no override) to catch
    regressions where /health/live is accidentally removed from _HEALTH_PATHS.
    """
    import jarvis_common.auth as _auth
    from fastapi import Depends, FastAPI
    from jarvis_common.auth import verify_api_key
    from jarvis_common.health import register_health_routes
    from jarvis_common.settings import get_secrets_settings

    # Set a real-looking API key so verify_api_key enforces key checks and
    # does not fall through to the no-key dev-bypass path.
    test_key = "a" * 32
    monkeypatch.setenv("JARVIS_API_KEY", test_key)
    get_secrets_settings.cache_clear()
    _auth.refresh_api_key_cache()

    # Build a minimal app with the real global dependency — mirrors production
    # wiring in learning_engine/main.py which passes dependencies=[Depends(verify_api_key)].
    minimal_app = FastAPI(dependencies=[Depends(verify_api_key)])
    register_health_routes(minimal_app, service_name="test", checks=[])

    async with httpx.AsyncClient(
        transport=ASGITransport(app=minimal_app), base_url="http://test"
    ) as client:
        resp_live = await client.get("/health/live")
        resp_internal = await client.get("/health/internal")

    # /health/live: no auth required — must be 200 (HEALTH-LIVE-403 fix).
    assert resp_live.status_code == 200, (
        f"/health/live returned {resp_live.status_code} without auth — "
        "check that /health/live is in auth._HEALTH_PATHS"
    )
    assert resp_live.json()["status"] == "ok"

    # /health/internal: always requires verify_api_key — must be 403 without key.
    assert resp_internal.status_code == 403, (
        f"/health/internal returned {resp_internal.status_code} without auth — "
        "/health/internal must NOT be exempt from API key auth"
    )
