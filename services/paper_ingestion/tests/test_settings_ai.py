"""Unit tests for /api/settings/ai endpoints (Wave 3-C, Tasks 21-23).

Pattern mirrors test_logs_admin_gate.py:
- Uses RoleMiddleware to inject request.state.user_role
- Uses httpx.AsyncClient + ASGITransport (no TestClient)
- Bypasses verify_api_key + rate-limiter via dependency_overrides
- Mocks DB pool via get_db_pool override
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest
from httpx import ASGITransport
from jarvis_common.auth import verify_api_key
from jarvis_common.testing import RoleMiddleware

from paper_ingestion.deps import get_db_pool
from paper_ingestion.main import app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def _base_app(mock_db):
    """Return (app, pool, conn) with rate-limiter + verify_api_key bypassed."""
    pool, conn = mock_db
    app.state.db_pool = pool
    app.state.limiter.enabled = False
    app.dependency_overrides[get_db_pool] = lambda: pool
    app.dependency_overrides[verify_api_key] = lambda: None
    yield app, pool, conn
    app.dependency_overrides.clear()
    app.state.limiter.enabled = True


def _admin_client(app) -> httpx.AsyncClient:
    """Wrap *app* with admin role injection and return an AsyncClient."""
    wrapped = RoleMiddleware(app, "admin")
    return httpx.AsyncClient(transport=ASGITransport(app=wrapped), base_url="http://test")


def _anon_client(app) -> httpx.AsyncClient:
    """Return an AsyncClient with NO role injected (simulates unauthenticated)."""
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ---------------------------------------------------------------------------
# Task 21: GET /api/settings/ai
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_settings_ai_returns_candidates(_base_app, monkeypatch):
    app, _pool, _conn = _base_app
    monkeypatch.setenv("JARVIS_HW_TIER", "ge-48")
    monkeypatch.setenv("JARVIS_LLM_BACKEND", "vllm")
    monkeypatch.setenv("JARVIS_SMART_MODEL", "Qwen/Qwen3-8B-AWQ")

    async with _admin_client(app) as client:
        resp = await client.get("/api/settings/ai")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["hw_tier"] == "ge-48"
    assert body["configured_backend"] == "vllm"
    assert body["configured_model"] == "Qwen/Qwen3-8B-AWQ"
    assert isinstance(body["candidates_for_tier"], list)
    assert len(body["candidates_for_tier"]) > 0


@pytest.mark.asyncio
async def test_get_settings_ai_requires_admin(_base_app):
    app, _pool, _conn = _base_app

    async with _anon_client(app) as client:
        resp = await client.get("/api/settings/ai")

    assert resp.status_code in (401, 403), resp.text


# ---------------------------------------------------------------------------
# Task 22: POST /api/settings/ai
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_settings_ai_rejects_non_curated_model(_base_app, monkeypatch):
    app, _pool, _conn = _base_app
    monkeypatch.setenv("JARVIS_HW_TIER", "ge-48")

    async with _admin_client(app) as client:
        resp = await client.post(
            "/api/settings/ai",
            json={"backend": "vllm", "model": "evil/unknown-model"},
        )

    assert resp.status_code == 422, resp.text
    assert "not in candidates_for_tier" in resp.json().get("detail", "")


@pytest.mark.asyncio
async def test_post_settings_ai_happy_path(_base_app, monkeypatch, tmp_path):
    app, _pool, _conn = _base_app
    monkeypatch.setenv("JARVIS_HW_TIER", "ge-48")

    calls: list[list[str]] = []

    def fake_run(*args, **kwargs):
        calls.append(list(args[0]))

        class _R:
            returncode = 0
            stdout = b""
            stderr = b""

        return _R()

    monkeypatch.setattr("subprocess.run", fake_run)

    class _FakeResp:
        def read(self):
            return b""

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **kw: _FakeResp())

    async with _admin_client(app) as client:
        resp = await client.post(
            "/api/settings/ai",
            json={"backend": "vllm", "model": "Qwen/Qwen3-14B-AWQ"},
        )

    assert resp.status_code == 200, resp.text
    assert any("render-litellm-config.sh" in " ".join(map(str, c)) for c in calls), (
        f"render-litellm-config.sh not called; calls={calls}"
    )
    assert any(
        "docker" in " ".join(map(str, c)) and "compose" in " ".join(map(str, c)) for c in calls
    ), f"docker compose not called; calls={calls}"


# ---------------------------------------------------------------------------
# Task 23: POST /api/settings/ai/redetect + dismiss-banner
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_redetect_returns_settings(_base_app, monkeypatch):
    app, _pool, _conn = _base_app
    monkeypatch.setenv("JARVIS_HW_TIER", "8-16")

    async with _admin_client(app) as client:
        resp = await client.post("/api/settings/ai/redetect")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["hw_tier"] == "8-16"
    assert "candidates_for_tier" in body


@pytest.mark.asyncio
async def test_redetect_requires_admin(_base_app):
    app, _pool, _conn = _base_app

    async with _anon_client(app) as client:
        resp = await client.post("/api/settings/ai/redetect")

    assert resp.status_code in (401, 403), resp.text


@pytest.mark.asyncio
async def test_dismiss_banner_inserts_event(_base_app):
    app, _pool, conn = _base_app
    conn.execute = AsyncMock(return_value="INSERT 0 1")

    async with _admin_client(app) as client:
        resp = await client.post(
            "/api/settings/ai/dismiss-banner",
            json={"banner_kind": "hw-upgrade-available"},
        )

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True}
    assert conn.execute.called
    # message arg carries the banner-kind so the dismissal is auditable
    assert "banner dismissed" in conn.execute.call_args[0][4]
    assert "hw-upgrade-available" in conn.execute.call_args[0][4]


@pytest.mark.asyncio
async def test_dismiss_banner_requires_admin(_base_app):
    app, _pool, _conn = _base_app

    async with _anon_client(app) as client:
        resp = await client.post(
            "/api/settings/ai/dismiss-banner",
            json={"banner_kind": "hw-upgrade-available"},
        )

    assert resp.status_code in (401, 403), resp.text
