"""Tests for GET /api/papers/brief and GET /api/system/models endpoints.

Covers:
- GET /api/papers/brief — lightweight paper list for dropdown selectors
- GET /api/system/models — installed Ollama models + config assignments
"""

from unittest.mock import AsyncMock, MagicMock

# conftest.py has already installed tiktoken / qdrant_client / qdrant_client.models /
# rapidfuzz / python_multipart stubs.
import httpx
import pytest
from httpx import ASGITransport

from tests.conftest import FakeRecord, _make_pool_and_conn

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class MockResponse:
    """Minimal HTTP response stub for mocking httpx calls."""

    def __init__(self, status_code: int, json_data: dict | None = None):
        self.status_code = status_code
        self._json = json_data or {}

    def json(self):
        return self._json


def _make_request(mock_pool, mock_http):
    """Create a minimal request stub with app state for direct handler calls."""
    request = MagicMock()
    request.app.state.db_pool = mock_pool
    request.app.state.http_client = mock_http
    return request


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def _app():
    """Create a minimal app instance with mocked DB pool, HTTP client, and disabled auth."""
    from jarvis_common import verify_api_key
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app

    mock_pool, conn = _make_pool_and_conn()
    app.state.db_pool = mock_pool
    app.state.limiter.enabled = False
    mock_http = AsyncMock()
    app.state.http_client = mock_http

    app.dependency_overrides[get_db_pool] = lambda: mock_pool
    app.dependency_overrides[verify_api_key] = lambda: None
    yield app, conn, mock_http
    app.dependency_overrides.clear()
    app.state.limiter.enabled = True


# ---------------------------------------------------------------------------
# Tests: GET /api/papers/brief
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_papers_brief_returns_list(_app):
    """GET /api/papers/brief returns a list of lightweight paper dicts."""
    app, conn, _ = _app
    conn.fetch.return_value = [
        FakeRecord(id=1, title="Paper One", source_type="arxiv", published_date=None),
        FakeRecord(
            id=2,
            title="Paper Two",
            source_type="semantic_scholar",
            published_date="2026-01-15",
        ),
    ]

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/papers/brief")

    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) == 2
    assert body[0]["title"] == "Paper One"
    assert body[1]["source_type"] == "semantic_scholar"


@pytest.mark.asyncio
async def test_papers_brief_with_search_filter(_app):
    """GET /api/papers/brief?search=transformer passes ILIKE filter to SQL."""
    app, conn, _ = _app
    conn.fetch.return_value = [
        FakeRecord(id=1, title="Transformer Model", source_type="arxiv", published_date=None),
    ]

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/papers/brief", params={"search": "transformer"})

    assert resp.status_code == 200

    # Verify SQL contains ILIKE for title filtering
    fetch_call = conn.fetch.call_args
    sql = fetch_call[0][0]
    assert "ILIKE" in sql


@pytest.mark.asyncio
async def test_papers_brief_without_search(_app):
    """GET /api/papers/brief without search param does NOT use ILIKE."""
    app, conn, _ = _app
    conn.fetch.return_value = []

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/papers/brief")

    assert resp.status_code == 200

    fetch_call = conn.fetch.call_args
    sql = fetch_call[0][0]
    assert "ILIKE" not in sql


@pytest.mark.asyncio
async def test_papers_brief_limit_200(_app):
    """GET /api/papers/brief SQL contains LIMIT 200."""
    app, conn, _ = _app
    conn.fetch.return_value = []

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/papers/brief")

    assert resp.status_code == 200

    fetch_call = conn.fetch.call_args
    sql = fetch_call[0][0]
    assert "LIMIT 200" in sql


@pytest.mark.asyncio
async def test_papers_brief_empty(_app):
    """GET /api/papers/brief returns empty list when no papers exist."""
    app, conn, _ = _app
    conn.fetch.return_value = []

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/papers/brief")

    assert resp.status_code == 200
    body = resp.json()
    assert body == []


# ---------------------------------------------------------------------------
# Tests: GET /api/system/models
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_system_models_full_response(_app):
    """GET /api/system/models returns installed, hardware, and current keys."""
    app, conn, mock_http = _app
    from paper_ingestion.main import get_system_models

    request = _make_request(app.state.db_pool, mock_http)

    # Mock config DB fetch: return LLM config entries
    conn.fetch.return_value = [
        FakeRecord(key="llm.smart_model", value="qwen3:4b"),
        FakeRecord(key="llm.fast_model", value="qwen3:4b"),
        FakeRecord(key="llm.embed_model", value="qwen3-embedding:0.6b"),
    ]

    async def mock_get_side_effect(url, **kwargs):
        if "/api/tags" in str(url):
            return MockResponse(
                200,
                {
                    "models": [
                        {
                            "name": "qwen3:4b",
                            "size": 4100000000,
                            "details": {
                                "parameter_size": "4B",
                                "quantization_level": "Q4_0",
                            },
                        },
                        {
                            "name": "qwen3-embedding:0.6b",
                            "size": 600000000,
                            "details": {
                                "parameter_size": "0.6B",
                                "quantization_level": "Q8_0",
                            },
                        },
                    ]
                },
            )
        elif "/api/ps" in str(url):
            return MockResponse(200, {"models": [{"name": "qwen3:4b"}]})
        return MockResponse(404)

    mock_http.get.side_effect = mock_get_side_effect

    body = await get_system_models(request)

    # Top-level keys (SystemModelsResponse is a Pydantic model — use attribute access)
    assert body.status == "ok"
    assert body.installed is not None
    assert body.hardware is not None
    assert body.current is not None
    assert body.issues == {}

    # Installed models
    assert len(body.installed) == 2
    assert body.installed[0]["name"] == "qwen3:4b"
    assert body.installed[0]["parameter_size"] == "4B"
    assert body.installed[0]["quantization"] == "Q4_0"

    # Hardware info
    assert body.hardware["ollama_running"] == 1

    # Current config assignments (key stripped of 'llm.' prefix)
    assert body.current["smart_model"] == "qwen3:4b"
    assert body.current["fast_model"] == "qwen3:4b"
    assert body.current["embed_model"] == "qwen3-embedding:0.6b"
    assert any(item["id"] == "qwen3-embedding:0.6b" for item in body.catalog)
    assert any(item["status"] == "active" for item in body.catalog)
    assert "embed" in body.recommendations

    # Delivery state (additive field): no llm.delivery_pending row → all applied
    assert body.delivery == {"smart": "applied", "fast": "applied", "embed": "applied"}


@pytest.mark.asyncio
async def test_system_models_ollama_unreachable(_app):
    """GET /api/system/models returns empty installed list when Ollama is down."""
    app, conn, mock_http = _app
    from paper_ingestion.main import get_system_models

    request = _make_request(app.state.db_pool, mock_http)

    conn.fetch.return_value = []
    mock_http.get.side_effect = httpx.ConnectError("Connection refused")

    body = await get_system_models(request)
    assert body.status == "degraded"
    assert body.installed == []
    assert "ollama_running" not in body.hardware
    assert body.issues == {
        "installed": "Could not load installed Ollama models.",
        "runtime": "Could not load Ollama runtime status.",
    }


@pytest.mark.asyncio
async def test_system_models_reports_embedding_config_mismatch(_app, monkeypatch):
    """GET /api/system/models surfaces stale model/dimension env drift."""
    app, conn, mock_http = _app
    from paper_ingestion.main import get_system_models

    monkeypatch.setattr(
        "paper_ingestion.routers.system.EMBEDDING_MODEL_NAME",
        "qwen3-embedding:0.6b",
    )
    monkeypatch.setattr("paper_ingestion.routers.system.EMBEDDING_DIMENSION", 768)

    request = _make_request(app.state.db_pool, mock_http)
    conn.fetch.return_value = []

    async def mock_get_side_effect(url, **kwargs):
        if "/api/tags" in str(url):
            return MockResponse(200, {"models": []})
        if "/api/ps" in str(url):
            return MockResponse(200, {"models": []})
        return MockResponse(404)

    mock_http.get.side_effect = mock_get_side_effect

    body = await get_system_models(request)

    assert body.status == "degraded"
    assert "embedding_config" in body.issues
    assert "qwen3-embedding:0.6b outputs 1024 dimensions" in body.issues["embedding_config"]


@pytest.mark.asyncio
async def test_system_models_no_config(_app):
    """GET /api/system/models returns empty current dict when no config exists."""
    app, conn, mock_http = _app
    from paper_ingestion.main import get_system_models

    request = _make_request(app.state.db_pool, mock_http)

    # DB returns no config rows
    conn.fetch.return_value = []

    # Ollama returns models but no running ones
    async def mock_get_side_effect(url, **kwargs):
        if "/api/tags" in str(url):
            return MockResponse(
                200,
                {
                    "models": [
                        {
                            "name": "llama3",
                            "size": 3000000000,
                            "details": {
                                "parameter_size": "3B",
                                "quantization_level": "Q4_K_M",
                            },
                        }
                    ]
                },
            )
        elif "/api/ps" in str(url):
            return MockResponse(200, {"models": []})
        return MockResponse(404)

    mock_http.get.side_effect = mock_get_side_effect

    body = await get_system_models(request)
    assert body.status == "ok"
    assert body.current == {}
    assert len(body.installed) == 1
    assert body.hardware["ollama_running"] == 0
    assert body.issues == {}


@pytest.mark.asyncio
async def test_system_models_db_failure_still_returns_ollama_data(_app):
    """GET /api/system/models degrades when config loading fails but still returns Ollama data."""
    app, conn, mock_http = _app
    from paper_ingestion.main import get_system_models

    request = _make_request(app.state.db_pool, mock_http)

    conn.fetch.side_effect = RuntimeError("db unavailable")

    async def mock_get_side_effect(url, **kwargs):
        if "/api/tags" in str(url):
            return MockResponse(200, {"models": [{"name": "llama3", "details": {}}]})
        if "/api/ps" in str(url):
            return MockResponse(200, {"models": [{"name": "llama3"}]})
        return MockResponse(404)

    mock_http.get.side_effect = mock_get_side_effect

    body = await get_system_models(request)
    assert body.status == "degraded"
    assert body.current == {}
    assert len(body.installed) == 1
    assert body.hardware["ollama_running"] == 1
    assert body.issues == {
        "current": "Could not load current model assignments.",
    }


@pytest.mark.asyncio
async def test_system_models_surfaces_pending_delivery(_app):
    """GET /api/system/models maps llm.delivery_pending roles to pending_restart."""
    app, conn, mock_http = _app
    from paper_ingestion.main import get_system_models

    request = _make_request(app.state.db_pool, mock_http)

    async def fetch_side_effect(sql, *args):
        keys = args[0] if args else []
        if "llm.delivery_pending" in keys:
            return [FakeRecord(key="llm.delivery_pending", value=["smart"])]
        return [FakeRecord(key="llm.smart_model", value="qwen3:4b")]

    conn.fetch.side_effect = fetch_side_effect
    mock_http.get.side_effect = httpx.ConnectError("Connection refused")

    body = await get_system_models(request)

    assert body.delivery == {
        "smart": "pending_restart",
        "fast": "applied",
        "embed": "applied",
    }


@pytest.mark.asyncio
async def test_system_models_delivery_empty_when_config_read_fails(_app):
    """Delivery state read failure leaves delivery empty — never a phantom applied."""
    app, conn, mock_http = _app
    from paper_ingestion.main import get_system_models

    request = _make_request(app.state.db_pool, mock_http)
    conn.fetch.side_effect = RuntimeError("db unavailable")
    mock_http.get.side_effect = httpx.ConnectError("Connection refused")

    body = await get_system_models(request)

    assert body.delivery == {}


@pytest.mark.asyncio
async def test_system_models_runtime_probe_failure_keeps_installed_models(_app):
    """GET /api/system/models keeps installed models even when runtime probe fails."""
    app, conn, mock_http = _app
    from paper_ingestion.main import get_system_models

    request = _make_request(app.state.db_pool, mock_http)

    conn.fetch.return_value = []

    async def mock_get_side_effect(url, **kwargs):
        if "/api/tags" in str(url):
            return MockResponse(200, {"models": [{"name": "mistral", "details": {}}]})
        if "/api/ps" in str(url):
            raise httpx.ReadTimeout("timed out")
        return MockResponse(404)

    mock_http.get.side_effect = mock_get_side_effect

    body = await get_system_models(request)
    assert body.status == "degraded"
    assert len(body.installed) == 1
    assert "ollama_running" not in body.hardware
    assert body.issues == {
        "runtime": "Could not load Ollama runtime status.",
    }


@pytest.mark.asyncio
async def test_model_recommendations_endpoint_returns_role_catalog(_app):
    """GET /api/system/models/recommendations returns role-filtered catalog entries."""
    app, conn, mock_http = _app
    from paper_ingestion.routers.system import get_model_recommendations

    request = _make_request(app.state.db_pool, mock_http)
    conn.fetch.return_value = [FakeRecord(key="llm.embed_model", value="embed")]

    async def mock_get_side_effect(url, **kwargs):
        if "/api/tags" in str(url):
            return MockResponse(200, {"models": [{"name": "qwen3-embedding:4b"}]})
        if "/api/ps" in str(url):
            return MockResponse(200, {"models": []})
        return MockResponse(404)

    mock_http.get.side_effect = mock_get_side_effect

    body = await get_model_recommendations(request, role="embed")

    assert body["role"] == "embed"
    assert body["recommendations"][0]["id"] == "qwen3-embedding:4b"
    assert body["recommendations"][0]["status"] == "active"


@pytest.mark.asyncio
async def test_pull_system_model_enqueues_model_pull_job(_app, monkeypatch):
    """POST /api/system/models/{tag}/pull enqueues a model.pull job."""
    app, _conn, mock_http = _app
    import jarvis_common.task_registry as task_registry
    from paper_ingestion.routers.system import pull_system_model

    request = _make_request(app.state.db_pool, mock_http)
    fake_task = MagicMock()
    fake_task.defer_async = AsyncMock()
    monkeypatch.setitem(task_registry._TASK_MAP, "model.pull", fake_task)

    body = await pull_system_model("qwen3:4b", request, user_id=None)

    assert body.status == "queued"
    fake_task.defer_async.assert_awaited_once()
    assert fake_task.defer_async.await_args.kwargs["ollama_tag"] == "qwen3:4b"


@pytest.mark.asyncio
async def test_delete_system_model_rejects_active_assignment(_app):
    """DELETE /api/system/models/{tag} rejects currently assigned models."""
    app, conn, mock_http = _app
    from fastapi import HTTPException
    from paper_ingestion.routers.system import delete_system_model

    request = _make_request(app.state.db_pool, mock_http)
    conn.fetch.return_value = [FakeRecord(key="llm.smart_model", value="qwen3:4b")]

    async def mock_get_side_effect(url, **kwargs):
        if "/api/tags" in str(url):
            return MockResponse(200, {"models": [{"name": "qwen3:4b"}]})
        if "/api/ps" in str(url):
            return MockResponse(200, {"models": []})
        return MockResponse(404)

    mock_http.get.side_effect = mock_get_side_effect

    with pytest.raises(HTTPException) as exc:
        await delete_system_model("qwen3:4b", request)

    assert exc.value.status_code == 409
    mock_http.request.assert_not_called()


@pytest.mark.asyncio
async def test_delete_system_model_calls_ollama_delete_for_inactive_model(_app, monkeypatch):
    """DELETE /api/system/models/{tag} proxies inactive models to Ollama delete."""
    app, conn, mock_http = _app
    from paper_ingestion.routers.system import delete_system_model

    monkeypatch.setenv("OLLAMA_BASE_URL", "http://ollama:11434")
    request = _make_request(app.state.db_pool, mock_http)
    conn.fetch.return_value = [FakeRecord(key="llm.smart_model", value="qwen3:4b")]

    async def mock_get_side_effect(url, **kwargs):
        if "/api/tags" in str(url):
            return MockResponse(200, {"models": [{"name": "qwen3:8b"}]})
        if "/api/ps" in str(url):
            return MockResponse(200, {"models": []})
        return MockResponse(404)

    mock_http.get.side_effect = mock_get_side_effect
    mock_http.request.return_value = MockResponse(200, {"status": "success"})

    body = await delete_system_model("qwen3:8b", request)

    assert body.status_code == 204
    mock_http.request.assert_awaited_once()
    assert mock_http.request.await_args.args[:2] == (
        "DELETE",
        "http://ollama:11434/api/delete",
    )
    assert mock_http.request.await_args.kwargs["json"] == {"name": "qwen3:8b"}


@pytest.mark.asyncio
async def test_delete_system_model_rejects_non_catalog_model(_app):
    """DELETE /api/system/models/{tag} must not delete arbitrary Ollama tags."""
    app, conn, mock_http = _app
    from fastapi import HTTPException
    from paper_ingestion.routers.system import delete_system_model

    request = _make_request(app.state.db_pool, mock_http)
    conn.fetch.return_value = [FakeRecord(key="llm.smart_model", value="qwen3:4b")]

    with pytest.raises(HTTPException) as exc:
        await delete_system_model("llama3:latest", request)

    assert exc.value.status_code == 404
    mock_http.request.assert_not_called()


@pytest.mark.asyncio
async def test_delete_system_model_fails_closed_when_current_assignments_unavailable(_app):
    """DELETE must not proceed when active model assignments cannot be verified."""
    app, conn, mock_http = _app
    from fastapi import HTTPException
    from paper_ingestion.routers.system import delete_system_model

    request = _make_request(app.state.db_pool, mock_http)
    conn.fetch.side_effect = RuntimeError("database unavailable")

    async def mock_get_side_effect(url, **kwargs):
        if "/api/tags" in str(url):
            return MockResponse(200, {"models": [{"name": "qwen3:8b"}]})
        if "/api/ps" in str(url):
            return MockResponse(200, {"models": []})
        return MockResponse(404)

    mock_http.get.side_effect = mock_get_side_effect

    with pytest.raises(HTTPException) as exc:
        await delete_system_model("qwen3:8b", request)

    assert exc.value.status_code == 503
    mock_http.request.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: hardware_recommendation field in GET /api/system/models
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_system_models_includes_hardware_recommendation(_app, monkeypatch):
    """GET /api/system/models response includes hardware_recommendation field."""
    app, conn, mock_http = _app
    from paper_ingestion.main import get_system_models

    # Patch hardware detection to return a known 16 GB value so we can assert
    # deterministic recommendation output without an actual GPU.
    from paper_ingestion.services import model_lifecycle as ml

    monkeypatch.setattr(
        ml,
        "detect_hardware",
        lambda: ml.HardwareInfo(
            vram_gb=16.0,
            vram_source="nvidia-smi",
            tier=2,
            detected_at="2026-05-18T00:00:00+00:00",
            machine_id="test-host",
        ),
    )
    # Clear cached hw_info so the patched detect_hardware is called.
    if hasattr(app.state, "hw_info"):
        del app.state.hw_info
    if hasattr(app.state, "hw_info_at"):
        del app.state.hw_info_at

    request = _make_request(app.state.db_pool, mock_http)
    conn.fetch.return_value = []

    async def mock_get_side_effect(url, **kwargs):
        if "/api/tags" in str(url):
            return MockResponse(200, {"models": []})
        if "/api/ps" in str(url):
            return MockResponse(200, {"models": []})
        return MockResponse(404)

    mock_http.get.side_effect = mock_get_side_effect

    body = await get_system_models(request)

    # hardware_recommendation must be present and non-empty
    assert hasattr(body, "hardware_recommendation")
    hw_rec = body.hardware_recommendation
    assert isinstance(hw_rec, dict)
    assert hw_rec["bucket"] == "MID"
    assert "aliases" in hw_rec
    assert hw_rec["summary"]

    # 16 GB → qwen3:8b for smart (current config.yaml active default)
    aliases_by_name = {a["alias"]: a for a in hw_rec["aliases"]}
    assert aliases_by_name["smart"]["model"] == "qwen3:8b"
    assert aliases_by_name["fast"]["model"] == "qwen3:4b"
    assert aliases_by_name["embed"]["model"] == "qwen3-embedding:4b"
    # Measured defaults — not flagged as unverified
    assert aliases_by_name["smart"]["confirm_on_target"] is False


@pytest.mark.asyncio
async def test_system_models_hardware_recommendation_cpu_only_when_no_gpu(_app, monkeypatch):
    """hardware_recommendation is safe / non-crashing when no GPU detected."""
    app, conn, mock_http = _app
    from paper_ingestion.main import get_system_models
    from paper_ingestion.services import model_lifecycle as ml

    # Simulate CPU-only / GPU probe failure: vram_gb == 0.0
    monkeypatch.setattr(
        ml,
        "detect_hardware",
        lambda: ml.HardwareInfo(
            vram_gb=0.0,
            vram_source="cpu",
            tier=0,
            detected_at="2026-05-18T00:00:00+00:00",
            machine_id="test-host",
        ),
    )
    if hasattr(app.state, "hw_info"):
        del app.state.hw_info
    if hasattr(app.state, "hw_info_at"):
        del app.state.hw_info_at

    request = _make_request(app.state.db_pool, mock_http)
    conn.fetch.return_value = []

    async def mock_get_side_effect(url, **kwargs):
        if "/api/tags" in str(url):
            return MockResponse(200, {"models": []})
        if "/api/ps" in str(url):
            return MockResponse(200, {"models": []})
        return MockResponse(404)

    mock_http.get.side_effect = mock_get_side_effect

    body = await get_system_models(request)

    hw_rec = body.hardware_recommendation
    # vram_gb=0 maps to None in recommend_models call → probe-failure path
    assert hw_rec["bucket"] == "CPU_ONLY"
    # probe-failure path: empty aliases list (distinguishes no-GPU from tiny-GPU)
    assert hw_rec["aliases"] == []
    assert hw_rec["summary"]


@pytest.mark.asyncio
async def test_system_models_hardware_recommendation_48gb_no_confirm_flag(_app, monkeypatch):
    """hardware_recommendation for 48 GB GPU: smart is live-validated, no confirm flag."""
    app, conn, mock_http = _app
    from paper_ingestion.main import get_system_models
    from paper_ingestion.services import model_lifecycle as ml

    # Simulate 48 GB GPU (49 152 MiB → HIGH bucket)
    monkeypatch.setattr(
        ml,
        "detect_hardware",
        lambda: ml.HardwareInfo(
            vram_gb=48.0,
            vram_source="nvidia-smi",
            tier=4,
            detected_at="2026-05-18T00:00:00+00:00",
            machine_id="test-host",
        ),
    )
    if hasattr(app.state, "hw_info"):
        del app.state.hw_info
    if hasattr(app.state, "hw_info_at"):
        del app.state.hw_info_at

    request = _make_request(app.state.db_pool, mock_http)
    conn.fetch.return_value = []

    async def mock_get_side_effect(url, **kwargs):
        if "/api/tags" in str(url):
            return MockResponse(200, {"models": []})
        if "/api/ps" in str(url):
            return MockResponse(200, {"models": []})
        return MockResponse(404)

    mock_http.get.side_effect = mock_get_side_effect

    body = await get_system_models(request)

    hw_rec = body.hardware_recommendation
    assert hw_rec["bucket"] == "HIGH"

    aliases_by_name = {a["alias"]: a for a in hw_rec["aliases"]}
    assert aliases_by_name["smart"]["model"] == "qwen3:30b-a3b"
    # ≥40 GB recommendation is live-validated (48 GB deployment, 16k ctx, v0.7)
    assert aliases_by_name["smart"]["confirm_on_target"] is False


# ---------------------------------------------------------------------------
# Tests: routing truth (T1.3) — GET /api/system/models routing + consistent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_system_models_routing_mismatch_surfaces_inconsistency(_app, monkeypatch):
    """When LiteLLM routes a different model than stored intent, consistent=False + routing surfaced.

    Staged: llm.smart_model = "qwen3:4b" in DB; LiteLLM routes "qwen3:8b" for
    the smart alias. Expects routing["smart"] == "qwen3:8b" and consistent=False.
    """
    app, conn, mock_http = _app
    from paper_ingestion.main import get_system_models
    import paper_ingestion.services.litellm_config as _lc

    request = _make_request(app.state.db_pool, mock_http)

    conn.fetch.return_value = [
        FakeRecord(key="llm.smart_model", value="qwen3:4b"),
    ]
    mock_http.get.side_effect = httpx.ConnectError("no ollama")

    # LiteLLM routes smart → "ollama/qwen3:8b" (normalized: "qwen3:8b")
    async def _fake_deployments():
        return [
            {
                "model_name": "smart",
                "litellm_params": {"model": "ollama/qwen3:8b"},
                "model_info": {"id": "dep-1", "db_model": True},
            }
        ]

    monkeypatch.setattr(_lc, "get_litellm_deployments", _fake_deployments)

    body = await get_system_models(request)

    assert body.routing == {"smart": "qwen3:8b"}, f"unexpected routing: {body.routing}"
    assert body.consistent is False, "mismatch must set consistent=False"


@pytest.mark.asyncio
async def test_system_models_litellm_down_endpoint_stays_200(_app, monkeypatch):
    """When LiteLLM is unreachable, /models still returns 200 and degrades routing honestly.

    routing must be empty; consistent=False only when there is stored intent.
    """
    app, conn, mock_http = _app
    from paper_ingestion.main import get_system_models
    import paper_ingestion.services.litellm_config as _lc

    request = _make_request(app.state.db_pool, mock_http)

    conn.fetch.return_value = [
        FakeRecord(key="llm.smart_model", value="qwen3:8b"),
        FakeRecord(key="llm.fast_model", value="qwen3:4b"),
    ]
    mock_http.get.side_effect = httpx.ConnectError("no ollama")

    async def _deployments_fail():
        raise RuntimeError("LiteLLM /v1/model/info unreachable: connection refused")

    monkeypatch.setattr(_lc, "get_litellm_deployments", _deployments_fail)

    body = await get_system_models(request)

    # Endpoint must not crash
    assert body.routing == {}, f"routing must be empty when LiteLLM is down: {body.routing}"
    # There IS stored intent → cannot verify → not consistent
    assert body.consistent is False, (
        "consistent must be False when there is stored intent but LiteLLM is unreachable"
    )


@pytest.mark.asyncio
async def test_system_models_routing_consistent_when_litellm_matches(_app, monkeypatch):
    """When LiteLLM routes the same model as stored intent, consistent=True."""
    app, conn, mock_http = _app
    from paper_ingestion.main import get_system_models
    import paper_ingestion.services.litellm_config as _lc

    request = _make_request(app.state.db_pool, mock_http)

    conn.fetch.return_value = [
        FakeRecord(key="llm.smart_model", value="qwen3:8b"),
        FakeRecord(key="llm.fast_model", value="qwen3:4b"),
    ]
    mock_http.get.side_effect = httpx.ConnectError("no ollama")

    async def _fake_deployments():
        return [
            {
                "model_name": "smart",
                "litellm_params": {"model": "ollama/qwen3:8b"},
                "model_info": {"id": "dep-smart", "db_model": True},
            },
            {
                "model_name": "fast",
                "litellm_params": {"model": "ollama/qwen3:4b"},
                "model_info": {"id": "dep-fast", "db_model": True},
            },
        ]

    monkeypatch.setattr(_lc, "get_litellm_deployments", _fake_deployments)

    body = await get_system_models(request)

    assert body.routing.get("smart") == "qwen3:8b"
    assert body.routing.get("fast") == "qwen3:4b"
    assert body.consistent is True, (
        f"consistent must be True when routing matches; routing={body.routing}"
    )


@pytest.mark.asyncio
async def test_system_models_routing_consistent_with_latest_suffix(_app, monkeypatch):
    """:latest suffix on either side must not cause false divergence (consistent=True).

    Staged: DB stores "qwen3:8b"; LiteLLM reports "ollama/qwen3:8b:latest".
    The :latest-tolerant normalization must treat these as equal and set
    consistent=True.  Without the fix a direct-API-created row shows permanent
    false divergence.
    """
    app, conn, mock_http = _app
    from paper_ingestion.main import get_system_models
    import paper_ingestion.services.litellm_config as _lc

    request = _make_request(app.state.db_pool, mock_http)

    # DB intent: bare tag (no :latest)
    conn.fetch.return_value = [
        FakeRecord(key="llm.smart_model", value="qwen3:8b"),
    ]
    mock_http.get.side_effect = httpx.ConnectError("no ollama")

    # LiteLLM reports with :latest appended (common when created via direct API)
    async def _fake_deployments():
        return [
            {
                "model_name": "smart",
                "litellm_params": {"model": "ollama/qwen3:8b:latest"},
                "model_info": {"id": "dep-smart", "db_model": True},
            }
        ]

    monkeypatch.setattr(_lc, "get_litellm_deployments", _fake_deployments)

    body = await get_system_models(request)

    # routing must strip :latest before storing
    assert body.routing.get("smart") == "qwen3:8b", (
        f"routing must normalize :latest away; got {body.routing.get('smart')!r}"
    )
    # consistent must be True — no false divergence
    assert body.consistent is True, (
        f"consistent must be True when :latest is the only difference; routing={body.routing}"
    )
