"""Admin-gate tests for system.py routes.

Verifies that:
- DELETE /api/system/models/{tag}  requires admin or API-key
- POST /api/system/models/{tag}/pull requires admin or API-key
- GET /api/system/hardware          requires admin or API-key
- GET /api/system/models/recommendations requires admin or API-key

Pattern mirrors test_logs_admin_gate.py: RoleMiddleware injects
request.state.user_role; verify_api_key is bypassed via dependency_overrides
so only the role gate under test fires.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from httpx import ASGITransport
from jarvis_common.testing import RoleMiddleware
from jarvis_common.testing_contract_apps import (
    PITestAppOptions,
    patch_dependency_overrides,
    patch_pi_test_app,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def _base_app(mock_db, monkeypatch):
    """Return (app, pool, conn) with rate-limiter + verify_api_key bypassed."""
    from jarvis_common.auth import verify_api_key
    from paper_ingestion.deps import get_db_pool, limiter
    from paper_ingestion.main import app

    # The model-data handlers probe LiteLLM for real; point the probe at a
    # connection-refused address so its degraded path fails fast instead of
    # hanging ~10 s per test on resolving the compose-internal hostname.
    monkeypatch.setenv("LITELLM_BASE_URL", "http://127.0.0.1:9")

    pool, conn = mock_db
    with patch_pi_test_app(
        pool,
        app=app,
        get_db_pool=get_db_pool,
        limiter=limiter,
        options=PITestAppOptions(
            remove_owner_override=False,
            override_db_dependency=True,
            disable_limiter=True,
            # Tests below wire app.state.http_client themselves, and the
            # storage test asserts the degraded no-Qdrant section; declaring
            # both here guarantees they start absent and any in-test write is
            # removed again on exit.
            state_absent=("http_client", "qdrant_client"),
            dependency_overrides={verify_api_key: lambda: None},
        ),
    ):
        yield app, pool, conn


def _client_with_role(app, role: str | None) -> httpx.AsyncClient:
    """Wrap *app* in the role-injection middleware and return an AsyncClient."""
    wrapped = RoleMiddleware(app, role)
    return httpx.AsyncClient(transport=ASGITransport(app=wrapped), base_url="http://test")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fake_http_client(*, delete_status: int = 200) -> MagicMock:
    """Return a minimal fake http_client for Ollama calls."""
    http = MagicMock()
    # GET /api/tags → empty model list
    tags_resp = MagicMock()
    tags_resp.status_code = 200
    tags_resp.json.return_value = {"models": []}
    # GET /api/ps → empty running list
    ps_resp = MagicMock()
    ps_resp.status_code = 200
    ps_resp.json.return_value = {"models": []}
    http.get = AsyncMock(side_effect=[tags_resp, ps_resp])
    # DELETE /api/delete
    del_resp = MagicMock()
    del_resp.status_code = delete_status
    http.request = AsyncMock(return_value=del_resp)
    return http


# ---------------------------------------------------------------------------
# 1. DELETE /api/system/models/{tag}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_model_unauth_returns_401_or_403(_base_app):
    """No session role + no valid API key → 401 or 403."""
    app, _pool, _conn = _base_app

    import os

    original = os.environ.get("JARVIS_API_KEY")
    os.environ["JARVIS_API_KEY"] = "x" * 32  # ensure key guard is active
    from jarvis_common.auth import refresh_api_key_cache, verify_api_key

    refresh_api_key_cache()
    try:
        # Drop only the verify_api_key bypass so the global key guard fires;
        # the fixture's pool wiring and limiter state stay in place.
        with patch_dependency_overrides(app, remove_overrides={verify_api_key}):
            async with _client_with_role(app, None) as client:
                resp = await client.delete("/api/system/models/qwen3:4b")
    finally:
        if original is not None:
            os.environ["JARVIS_API_KEY"] = original
        else:
            os.environ.pop("JARVIS_API_KEY", None)
        refresh_api_key_cache()

    assert resp.status_code in (401, 403), resp.text


@pytest.mark.asyncio
async def test_delete_model_authed_non_admin_returns_403(_base_app):
    """Browser session with role='user' → 403 from require_admin_or_api_key."""
    app, _pool, _conn = _base_app

    async with _client_with_role(app, "user") as client:
        resp = await client.delete("/api/system/models/qwen3:4b")

    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_delete_model_admin_accepted(_base_app):
    """Admin session → gate passes; Ollama delete mock returns 200 → 204."""
    app, pool, conn = _base_app

    # Stub conn.fetch to return empty rows (no current model assignments).
    conn.fetch = AsyncMock(return_value=[])

    fake_http = _make_fake_http_client(delete_status=200)
    app.state.http_client = fake_http

    # Stub hardware probe so _get_system_models_data doesn't hit real hardware.
    from paper_ingestion.services.model_lifecycle import HardwareInfo

    fake_hw = HardwareInfo(
        vram_gb=0.0,
        vram_source="cpu",
        tier=0,
        detected_at="2026-01-01T00:00:00Z",
        machine_id="test-host",
    )
    with (
        patch(
            "paper_ingestion.services.system_models_view.async_get_cached_hardware",
            new=AsyncMock(return_value=fake_hw),
        ),
        patch(
            "paper_ingestion.routers.system.catalog_entry_for_model",
        ) as mock_entry,
        patch(
            "paper_ingestion.routers.system.normalize_model_tag",
            return_value="qwen3:4b",
        ),
    ):
        # Make the catalog entry look like a valid ollama model.
        entry = MagicMock()
        entry.provider = "ollama"
        entry.ollama_tag = "qwen3:4b"
        entry.id = "qwen3:4b"
        mock_entry.return_value = entry

        async with _client_with_role(app, "admin") as client:
            resp = await client.delete("/api/system/models/qwen3:4b")

    assert resp.status_code == 204, resp.text


# ---------------------------------------------------------------------------
# 2. POST /api/system/models/{tag}/pull
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pull_model_admin_only(_base_app):
    """Non-admin browser session → 403; admin session → call proceeds."""
    app, _pool, _conn = _base_app

    # Non-admin rejected.
    async with _client_with_role(app, "user") as client:
        resp = await client.post("/api/system/models/qwen3:4b/pull")

    assert resp.status_code == 403, resp.text

    # Admin accepted (catalog entry not found → 404, not 403 — gate passed).
    with patch(
        "paper_ingestion.routers.system.catalog_entry_for_model",
        return_value=None,
    ):
        async with _client_with_role(app, "admin") as client:
            resp = await client.post("/api/system/models/qwen3:4b/pull")

    # 404 means the gate passed and the handler ran (unknown model).
    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# 3. GET /api/system/hardware
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hardware_admin_only(_base_app):
    """Non-admin → 403; admin → call proceeds (hardware probe stubbed)."""
    app, _pool, _conn = _base_app

    # Non-admin rejected.
    async with _client_with_role(app, "user") as client:
        resp = await client.get("/api/system/hardware")

    assert resp.status_code == 403, resp.text

    # Admin accepted — stub hardware so no real probe runs.
    from paper_ingestion.services.model_lifecycle import HardwareInfo

    fake_hw = HardwareInfo(
        vram_gb=0.0,
        vram_source="cpu",
        tier=0,
        detected_at="2026-01-01T00:00:00Z",
        machine_id="test-host",
    )
    with patch(
        "paper_ingestion.routers.system.async_get_cached_hardware",
        new=AsyncMock(return_value=fake_hw),
    ):
        async with _client_with_role(app, "admin") as client:
            resp = await client.get("/api/system/hardware")

    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# 4. GET /api/system/models/recommendations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_model_recommendations_admin_only(_base_app):
    """Non-admin → 403; admin → call proceeds (heavy internals stubbed)."""
    app, _pool, conn = _base_app

    # Non-admin rejected.
    async with _client_with_role(app, "user") as client:
        resp = await client.get("/api/system/models/recommendations")

    assert resp.status_code == 403, resp.text

    # Admin accepted — stub everything that hits Ollama / hardware.
    conn.fetch = AsyncMock(return_value=[])

    fake_http = _make_fake_http_client()
    app.state.http_client = fake_http

    from paper_ingestion.services.model_lifecycle import HardwareInfo

    fake_hw = HardwareInfo(
        vram_gb=0.0,
        vram_source="cpu",
        tier=0,
        detected_at="2026-01-01T00:00:00Z",
        machine_id="test-host",
    )
    with patch(
        "paper_ingestion.services.system_models_view.async_get_cached_hardware",
        new=AsyncMock(return_value=fake_hw),
    ):
        async with _client_with_role(app, "admin") as client:
            resp = await client.get("/api/system/models/recommendations")

    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# 5. Helper-bypass: /recommendations must enforce auth at route level
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_model_recommendations_helper_not_publicly_callable_unauth(_base_app):
    """The /recommendations route rejects non-admin even though the underlying
    _get_system_models_data helper has no internal auth check.  The route-level
    Depends(require_admin_or_api_key) is the canonical gate."""
    app, _pool, _conn = _base_app

    # Calling /recommendations as a 'user' role must be rejected (403)
    # regardless of whether _get_system_models_data itself is public.
    async with _client_with_role(app, "user") as client:
        resp = await client.get("/api/system/models/recommendations")

    assert resp.status_code == 403, resp.text

    # Confirm _get_system_models_data is importable as a plain async function
    # (i.e. it exists and is not a route handler itself).
    from paper_ingestion.routers.system import _get_system_models_data

    import inspect

    assert inspect.iscoroutinefunction(_get_system_models_data)


# ---------------------------------------------------------------------------
# 6. GET /api/system/storage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_storage_admin_only(_base_app):
    """Non-admin → 403; admin → call proceeds (backends degrade, never 500)."""
    app, _pool, conn = _base_app

    # Non-admin rejected.
    async with _client_with_role(app, "user") as client:
        resp = await client.get("/api/system/storage")

    assert resp.status_code == 403, resp.text

    # Admin accepted — Ollama HTTP client stubbed; Postgres/Qdrant degrade
    # gracefully (no qdrant_client wired on app.state in this fixture).
    conn.fetchval = AsyncMock(return_value=0)
    app.state.http_client = _make_fake_http_client()

    async with _client_with_role(app, "admin") as client:
        resp = await client.get("/api/system/storage")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ollama_models"] == {"bytes_used": 0, "error": None}
    assert body["qdrant"]["error"] == "Qdrant client not available"
