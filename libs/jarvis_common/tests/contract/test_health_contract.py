"""Shared health contract suite (audit X-02).

Replaces the triplicated per-service health tests (~6-9 tests). One
parametrized suite over the 3 health surfaces asserting the shared
behavior: 200 + status field present + auth-not-required for public
routes.

Route contracts (from jarvis_common.health.register_health_routes and
telegram_bot.internal_api):

  GET /health/live  — no auth, no probes, always {"status": "ok"}, 200
  GET /health       — no auth, {"status": "ok"|"degraded"} only,
                      200 ok / 503 degraded
  GET /health/internal — requires verify_api_key, full HealthCheckResponse
                         {status, service, checks}, same 200/503 split

telegram_bot exposes GET /health on _internal_app (not register_health_routes).
It returns {"status": "ok"} unconditionally with no auth.  It is included in
the no-auth / status-present assertions only; the degraded and /health/internal
cases apply only to services using register_health_routes.

Per-service test_health_*.py files collapse to a thin
"is the route wired" smoke (one test per service), citing this suite as
the shared authoritative contract.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from httpx import ASGITransport

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clear_sweep_memo(app: Any) -> None:
    """Drop the short-TTL health sweep memo so a rewired app re-probes immediately.

    The contract tests reuse the module-level service ``app`` singleton and flip
    dependency state between cases; without this reset a prior cycle's cached
    status would leak into the next case within the memo TTL.
    """
    from jarvis_common.health import _SWEEP_MEMO_ATTR, _SWEEP_TASK_ATTR

    if hasattr(app.state, _SWEEP_MEMO_ATTR):
        delattr(app.state, _SWEEP_MEMO_ATTR)
    if hasattr(app.state, _SWEEP_TASK_ATTR):
        delattr(app.state, _SWEEP_TASK_ATTR)


def _make_mock_pool(*, raise_on_acquire: bool = False) -> MagicMock:
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=1)
    ctx = MagicMock()
    if raise_on_acquire:
        ctx.__aenter__ = AsyncMock(side_effect=RuntimeError("DB down"))
    else:
        ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx
    return pool


def _make_mock_http(*, healthy: bool = True) -> AsyncMock:
    """Mock httpx.AsyncClient whose .get() returns 200 (healthy) or raises (unhealthy)."""
    client = AsyncMock()
    if healthy:
        resp = MagicMock()
        resp.status_code = 200
        client.get = AsyncMock(return_value=resp)
    else:
        client.get = AsyncMock(side_effect=ConnectionError("dependency down"))
    return client


def _wire_pi_app(*, db_up: bool = True, http_healthy: bool = True) -> Any:
    """Return paper_ingestion.app with all state mocked; clears overrides on test exit."""
    from jarvis_common import verify_api_key
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app

    pool = _make_mock_pool(raise_on_acquire=not db_up)
    http = _make_mock_http(healthy=http_healthy)
    qdrant = MagicMock()
    qdrant.collection_exists = AsyncMock(return_value=True)

    app.state.db_pool = pool
    app.state.http_client = http
    app.state.qdrant_client = qdrant
    _clear_sweep_memo(app)
    try:
        app.state.limiter.enabled = False
    except AttributeError:
        pass

    app.dependency_overrides[get_db_pool] = lambda: pool
    app.dependency_overrides[verify_api_key] = lambda: None
    return app


def _wire_le_app(*, db_up: bool = True, http_healthy: bool = True) -> Any:
    """Return learning_engine.app with all state mocked."""
    from jarvis_common import verify_api_key
    from learning_engine.deps import get_db_pool
    from learning_engine.main import app

    pool = _make_mock_pool(raise_on_acquire=not db_up)
    http = _make_mock_http(healthy=http_healthy)

    app.state.db_pool = pool
    app.state.http_client = http
    _clear_sweep_memo(app)
    try:
        app.state.limiter.enabled = False
    except AttributeError:
        pass

    app.dependency_overrides[get_db_pool] = lambda: pool
    app.dependency_overrides[verify_api_key] = lambda: None
    return app


# ---------------------------------------------------------------------------
# Parametrised app fixture — covers the two register_health_routes services.
# telegram_bot._internal_app is handled separately below because it does not
# use register_health_routes and has no degraded path or /health/internal.
# ---------------------------------------------------------------------------


@pytest.fixture(
    params=[
        "paper_ingestion",
        "learning_engine",
    ]
)
def rhr_app(request: pytest.FixtureRequest):
    """Yield (service_name, wired_app) for services using register_health_routes."""
    name = request.param
    if name == "paper_ingestion":
        app = _wire_pi_app()
    else:
        app = _wire_le_app()
    yield name, app
    app.dependency_overrides.clear()
    try:
        app.state.limiter.enabled = True
    except AttributeError:
        pass


# ---------------------------------------------------------------------------
# Contract: GET /health — public, status-only
# ---------------------------------------------------------------------------


async def test_health_public_200_when_ok(rhr_app):
    """GET /health → 200 with status='ok' when all probes pass."""
    _name, app = rhr_app
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"


async def test_health_public_exposes_only_status(rhr_app):
    """GET /health must NOT expose service or checks keys."""
    _name, app = rhr_app
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/health")
    body = resp.json()
    assert "service" not in body
    assert "checks" not in body


async def test_health_public_503_when_db_down(rhr_app):
    """GET /health → 503 with status='degraded' and no checks when DB fails."""
    name, _app = rhr_app
    # Build a fresh wired app with DB down; avoid mutating the fixture's app
    if name == "paper_ingestion":
        app = _wire_pi_app(db_up=False)
    else:
        app = _wire_le_app(db_up=False)
    try:
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/health")
        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "degraded"
        assert "checks" not in body
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Contract: GET /health/live — always 200, no auth, no probes
# ---------------------------------------------------------------------------


async def test_health_live_always_200(rhr_app):
    """GET /health/live → 200 {"status": "ok"} unconditionally."""
    _name, app = rhr_app
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/health/live")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_health_live_no_auth_required(rhr_app, monkeypatch: pytest.MonkeyPatch):
    """GET /health/live returns 200 even without an API key (HEALTH-LIVE-403 regression guard).

    Builds a minimal app with real verify_api_key enforced so the _HEALTH_PATHS
    exemption is exercised rather than bypassed by the fixture's dependency override.
    """
    import jarvis_common.auth as _auth
    from fastapi import Depends, FastAPI
    from jarvis_common.auth import verify_api_key
    from jarvis_common.health import register_health_routes
    from jarvis_common.settings import get_secrets_settings

    test_key = "a" * 32
    monkeypatch.setenv("JARVIS_API_KEY", test_key)
    get_secrets_settings.cache_clear()
    _auth.refresh_api_key_cache()

    minimal_app = FastAPI(dependencies=[Depends(verify_api_key)])
    register_health_routes(minimal_app, service_name="test-live", checks=[])

    async with httpx.AsyncClient(
        transport=ASGITransport(app=minimal_app), base_url="http://test"
    ) as c:
        resp_live = await c.get("/health/live")
        resp_internal = await c.get("/health/internal")

    assert resp_live.status_code == 200, (
        f"/health/live returned {resp_live.status_code} without auth — "
        "check that /health/live is in auth._HEALTH_PATHS"
    )
    assert resp_live.json()["status"] == "ok"
    # /health/internal is NOT in _HEALTH_PATHS — must 403 without key
    assert resp_internal.status_code == 403, (
        f"/health/internal returned {resp_internal.status_code} without auth — "
        "/health/internal must NOT be exempt from API key auth"
    )


# ---------------------------------------------------------------------------
# Contract: GET /health/internal — authenticated, full HealthCheckResponse
# ---------------------------------------------------------------------------


async def test_health_internal_200_full_payload(rhr_app):
    """GET /health/internal → 200 with {status, service, checks} when all probes pass."""
    name, app = rhr_app
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/health/internal")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    # service matches the service_name passed to register_health_routes
    assert body["service"] == name
    assert "checks" in body
    assert isinstance(body["checks"], dict)
    # postgres probe is registered for both services
    assert body["checks"].get("postgres") == "ok"


async def test_health_internal_reports_app_version(rhr_app):
    """version field delegates to jarvis_common.version.app_version() — not a hardcoded literal.

    Compares against the canonical helper (rather than a literal like "1.0.4" or a
    duplicated ``importlib.metadata`` call) so the test is agnostic to whether the
    ``jarvis-rd-assistant`` distribution is installed/discoverable in the running
    environment; it still pins the historical ``0.1.0`` regression.
    """
    from jarvis_common.version import app_version

    _name, app = rhr_app
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/health/internal")
    body = resp.json()
    assert body["version"] == app_version()
    assert body["version"] != "0.1.0"


async def test_health_internal_503_when_db_down(rhr_app):
    """GET /health/internal → 503 with degraded status and checks dict when DB fails."""
    name, _app = rhr_app
    if name == "paper_ingestion":
        app = _wire_pi_app(db_up=False)
    else:
        app = _wire_le_app(db_up=False)
    try:
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/health/internal")
        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "degraded"
        assert "checks" in body
        assert body["checks"]["postgres"] == "unavailable"
    finally:
        app.dependency_overrides.clear()


async def test_health_internal_includes_maintenance_and_version(
    rhr_app, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """GET /health/internal exposes the maintenance flag (bool) and app version."""
    _name, app = rhr_app
    # Point the sentinels at an empty dir so a stray host sentinel can't flip this.
    monkeypatch.setenv("MAINTENANCE_SENTINEL", str(tmp_path / ".maintenance"))
    monkeypatch.setenv("MAINTENANCE_DESTRUCTIVE_SENTINEL", str(tmp_path / ".destructive"))
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/health/internal")
    body = resp.json()
    assert body["maintenance"] is False
    assert isinstance(body["version"], str)
    assert body["version"]


async def test_health_internal_reports_active_maintenance(
    rhr_app, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """A fresh maintenance sentinel surfaces as maintenance=true in /health/internal."""
    _name, app = rhr_app
    sentinel = tmp_path / ".maintenance"
    sentinel.touch()
    monkeypatch.setenv("MAINTENANCE_SENTINEL", str(sentinel))
    monkeypatch.setenv("MAINTENANCE_DESTRUCTIVE_SENTINEL", str(tmp_path / ".destructive"))
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/health/internal")
    assert resp.json()["maintenance"] is True


async def test_health_live_and_public_stay_minimal(rhr_app):
    """/health/live and /health never expose maintenance or version (probes parse them)."""
    _name, app = rhr_app
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        live_body = (await c.get("/health/live")).json()
        public_body = (await c.get("/health")).json()
    for body in (live_body, public_body):
        assert "maintenance" not in body
        assert "version" not in body


# ---------------------------------------------------------------------------
# telegram_bot._internal_app: health surface
#
# The bot exposes its health routes through register_health_routes, like the
# other services, with a postgres probe resolved from app.state.db_pool at call
# time. GET /health reports degraded (503) when that probe fails; GET
# /health/live stays 200 regardless, which is what the container healthcheck
# uses so a database blip cannot cascade through service_healthy dependents.
# Neither route requires an API key.
# ---------------------------------------------------------------------------


@pytest.fixture()
def tg_internal_app():
    from telegram_bot.internal_api import _internal_app

    # The probe reads app.state.db_pool when the route is called; without a pool
    # every request here would report degraded.
    _internal_app.state.db_pool = _make_mock_pool()
    return _internal_app


async def test_telegram_bot_health_200(tg_internal_app):
    """GET /health on telegram_bot._internal_app → 200 {"status": "ok"}."""
    async with httpx.AsyncClient(
        transport=ASGITransport(app=tg_internal_app), base_url="http://test"
    ) as c:
        resp = await c.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_telegram_bot_health_no_auth_required(tg_internal_app):
    """GET /health on telegram_bot._internal_app requires no API key."""
    # Call without any X-API-Key header — must still succeed
    async with httpx.AsyncClient(
        transport=ASGITransport(app=tg_internal_app), base_url="http://test"
    ) as c:
        resp = await c.get("/health", headers={})
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


# ---------------------------------------------------------------------------
# Aggregator behaviour: concurrency, per-request timeout, sweep memo (M4.2 / D3)
# ---------------------------------------------------------------------------


def _fake_request() -> Any:
    """Minimal stand-in for fastapi.Request exposing ``.app.state`` for probes/memo."""
    from types import SimpleNamespace

    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))


async def test_sweep_runs_concurrently() -> None:
    """_execute_sweep awaits all probes at once: wall time ≈ one probe, not the sum."""
    import asyncio
    import time

    from jarvis_common.health import _execute_sweep

    async def _slow(_request: Any) -> str:
        await asyncio.sleep(0.2)
        return "ok"

    checks = [("a", _slow), ("b", _slow), ("c", _slow)]
    start = time.monotonic()
    status, results = await _execute_sweep(_fake_request(), checks)
    elapsed = time.monotonic() - start

    assert status == "ok"
    assert results == {"a": "ok", "b": "ok", "c": "ok"}
    # Sequential would be ≥0.6s; concurrent stays well under the 0.6s sum.
    assert elapsed < 0.4, f"sweep took {elapsed:.3f}s — probes did not run concurrently"


async def test_sweep_maps_timeout_and_exception() -> None:
    """A probe over budget → 'timeout'; a probe that raises → 'unavailable'; rollup degraded."""
    import asyncio

    import jarvis_common.health as health

    async def _ok(_request: Any) -> str:
        return "ok"

    async def _hangs(_request: Any) -> str:
        await asyncio.sleep(10)
        return "ok"

    async def _raises(_request: Any) -> str:
        raise RuntimeError("boom")

    checks = [("good", _ok), ("slow", _hangs), ("broken", _raises)]
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(health, "_PROBE_TIMEOUT_S", 0.05)
        status, results = await health._execute_sweep(_fake_request(), checks)

    assert results == {"good": "ok", "slow": "timeout", "broken": "unavailable"}
    assert status == "degraded"


async def test_double_sweep_eliminated_within_ttl() -> None:
    """Two back-to-back run_health_checks on one app share a single probe run (D3)."""
    from jarvis_common.health import run_health_checks

    calls = {"n": 0}

    async def _counting(_request: Any) -> str:
        calls["n"] += 1
        return "ok"

    request = _fake_request()
    checks = [("dep", _counting)]
    first = await run_health_checks(request, checks)
    second = await run_health_checks(request, checks)

    assert first == second == ("ok", {"dep": "ok"})
    assert calls["n"] == 1, "probe ran more than once — sweep memo did not dedupe the paired poll"


async def test_concurrent_health_polls_share_in_flight_sweep() -> None:
    """Concurrent /health + /health/internal polling shares one in-flight probe sweep."""
    import asyncio

    from jarvis_common.health import run_health_checks

    calls = {"n": 0}
    release = asyncio.Event()

    async def _counting(_request: Any) -> str:
        calls["n"] += 1
        await release.wait()
        return "ok"

    request = _fake_request()
    checks = [("dep", _counting)]
    first = asyncio.create_task(run_health_checks(request, checks))
    second = asyncio.create_task(run_health_checks(request, checks))
    await asyncio.sleep(0)

    release.set()
    results = await asyncio.gather(first, second)

    assert results == [("ok", {"dep": "ok"}), ("ok", {"dep": "ok"})]
    assert calls["n"] == 1, "concurrent health polls should share one in-flight sweep"


async def test_cancelled_waiter_does_not_cancel_shared_sweep() -> None:
    """Cancelling one health waiter must not cancel the shared in-flight sweep."""
    import asyncio

    from jarvis_common.health import _SWEEP_MEMO_ATTR, run_health_checks

    calls = {"n": 0}
    release = asyncio.Event()

    async def _counting(_request: Any) -> str:
        calls["n"] += 1
        await release.wait()
        return "ok"

    request = _fake_request()
    checks = [("dep", _counting)]
    first = asyncio.create_task(run_health_checks(request, checks))
    second = asyncio.create_task(run_health_checks(request, checks))
    await asyncio.sleep(0)

    first.cancel()
    await asyncio.sleep(0)
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await first
    assert await second == ("ok", {"dep": "ok"})
    assert calls["n"] == 1, "probe must run exactly once despite the cancelled waiter"
    memo = getattr(request.app.state, _SWEEP_MEMO_ATTR, None)
    assert memo is not None and memo.status == "ok", "surviving waiter must populate the memo"


async def test_sweep_memo_expires_after_ttl() -> None:
    """After the TTL, run_health_checks re-probes rather than serving a stale memo."""
    import jarvis_common.health as health

    calls = {"n": 0}

    async def _counting(_request: Any) -> str:
        calls["n"] += 1
        return "ok"

    request = _fake_request()
    checks = [("dep", _counting)]
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(health, "_SWEEP_MEMO_TTL_S", 0.0)
        await health.run_health_checks(request, checks)
        await health.run_health_checks(request, checks)

    assert calls["n"] == 2, "memo with 0s TTL should not be reused"


async def test_sweep_memo_preserves_degraded() -> None:
    """A degraded sweep is memoized as degraded — never collapsed to ok within the TTL."""
    from jarvis_common.health import run_health_checks

    async def _down(_request: Any) -> str:
        return "unavailable"

    request = _fake_request()
    status, results = await run_health_checks(request, [("dep", _down)])
    cached_status, _ = await run_health_checks(request, [("dep", _down)])

    assert status == "degraded"
    assert results == {"dep": "unavailable"}
    assert cached_status == "degraded"


async def test_litellm_probe_uses_per_request_timeout() -> None:
    """make_litellm_probe issues client.get with an explicit ~2s per-request timeout."""
    from jarvis_common.health import make_litellm_probe
    from jarvis_common.llm_client import LiteLLMConfig

    resp = MagicMock()
    resp.status_code = 200
    client = AsyncMock()
    client.get = AsyncMock(return_value=resp)
    cfg = LiteLLMConfig(base_url="http://litellm:4000")

    probe = make_litellm_probe(http_client=client, config=cfg)
    assert await probe(_fake_request()) == "ok"

    _args, kwargs = client.get.call_args
    assert kwargs.get("timeout") == 2.0, "LiteLLM probe must pass an explicit per-request timeout"


async def test_litellm_probe_uses_dedicated_client_when_shared_pool_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default LiteLLM health probes retry outside the shared app HTTP pool."""
    import httpx
    from jarvis_common.health import make_litellm_probe
    from jarvis_common.llm_client import LiteLLMConfig

    calls = {"dedicated": 0, "shared": 0}

    class SharedClient:
        async def get(self, _url: str, *, timeout: float) -> object:
            calls["shared"] += 1
            raise httpx.ConnectTimeout("pool saturated")

    class Response:
        status_code = 200

    class DedicatedClient:
        async def __aenter__(self) -> DedicatedClient:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def get(self, _url: str, *, timeout: float) -> Response:
            calls["dedicated"] += 1
            assert timeout == 2.0
            return Response()

    monkeypatch.setattr(httpx, "AsyncClient", DedicatedClient)
    request = _fake_request()
    request.app.state.http_client = SharedClient()

    probe = make_litellm_probe(config=LiteLLMConfig(base_url="http://litellm:4000"))

    assert await probe(request) == "ok"
    assert calls == {"shared": 1, "dedicated": 1}


async def test_vector_probe_short_circuits_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """_probe_vector returns 'unknown' with no network round-trip when vector_api_url is empty."""
    import paper_ingestion.main as pi_main
    from paper_ingestion.config import PaperIngestionSettings

    settings = PaperIngestionSettings(vector_api_url="")
    monkeypatch.setattr(pi_main, "get_paper_ingestion_settings", lambda: settings)

    request = _fake_request()
    http = AsyncMock()
    http.get = AsyncMock(side_effect=AssertionError("vector probe must not make a request"))
    request.app.state.http_client = http

    assert await pi_main._probe_vector(request) == "unknown"
    http.get.assert_not_called()


async def test_paper_ingestion_qdrant_probe_checks_required_collection() -> None:
    """_probe_qdrant returns 'ok' only when the paper chunk collection exists."""
    import paper_ingestion.main as pi_main
    from paper_ingestion.ingestion.embedding_config import COLLECTION_NAME

    qdrant = MagicMock()
    qdrant.collection_exists = AsyncMock(return_value=True)
    request = _fake_request()
    request.app.state.qdrant_client = qdrant

    assert await pi_main._probe_qdrant(request) == "ok"
    qdrant.collection_exists.assert_awaited_once_with(COLLECTION_NAME)


async def test_paper_ingestion_qdrant_probe_missing_collection_degrades() -> None:
    """A missing required collection is a real dependency failure."""
    import paper_ingestion.main as pi_main

    qdrant = MagicMock()
    qdrant.collection_exists = AsyncMock(return_value=False)
    request = _fake_request()
    request.app.state.qdrant_client = qdrant

    assert await pi_main._probe_qdrant(request) == "unavailable"


async def test_paper_ingestion_qdrant_probe_timeout_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slow metadata call is visible but does not make public health return 503."""
    import asyncio

    import paper_ingestion.main as pi_main

    async def slow_probe(_collection_name: str) -> bool:
        await asyncio.sleep(0.01)
        return True

    monkeypatch.setattr(pi_main, "_QDRANT_HEALTH_TIMEOUT_S", 0.001)
    qdrant = MagicMock()
    qdrant.collection_exists = AsyncMock(side_effect=slow_probe)
    request = _fake_request()
    request.app.state.qdrant_client = qdrant

    assert await pi_main._probe_qdrant(request) == "unknown"


async def test_paper_ingestion_qdrant_probe_exception_degrades() -> None:
    """Non-timeout Qdrant exceptions remain degraded."""
    import paper_ingestion.main as pi_main

    qdrant = MagicMock()
    qdrant.collection_exists = AsyncMock(side_effect=RuntimeError("dependency down"))
    request = _fake_request()
    request.app.state.qdrant_client = qdrant

    assert await pi_main._probe_qdrant(request) == "unavailable"


async def test_paper_ingestion_qdrant_timeout_stays_within_outer_health_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Qdrant metadata slowness reports unknown, not outer-sweep timeout."""
    import asyncio

    import jarvis_common.health as health
    import paper_ingestion.main as pi_main

    async def slow_probe(_collection_name: str) -> bool:
        await asyncio.sleep(0.02)
        return True

    app = _wire_pi_app()
    monkeypatch.setattr(health, "_PROBE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(pi_main, "_QDRANT_HEALTH_TIMEOUT_S", 0.01)
    app.state.qdrant_client.collection_exists = AsyncMock(side_effect=slow_probe)
    _clear_sweep_memo(app)

    try:
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/health/internal")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["checks"]["qdrant"] == "unknown"
