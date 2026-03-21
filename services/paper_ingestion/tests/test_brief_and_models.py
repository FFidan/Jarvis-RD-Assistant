"""Tests for GET /api/papers/brief and GET /api/system/models endpoints.

Covers:
- GET /api/papers/brief — lightweight paper list for dropdown selectors
- GET /api/system/models — installed Ollama models + config assignments
"""

import sys
import types
from unittest.mock import AsyncMock, MagicMock

# Stub heavy/optional modules unavailable outside Docker.
if "fitz" not in sys.modules:
    sys.modules["fitz"] = MagicMock()

if "tiktoken" not in sys.modules:
    fake_tiktoken = types.ModuleType("tiktoken")
    fake_tiktoken.get_encoding = MagicMock(return_value=MagicMock())
    sys.modules["tiktoken"] = fake_tiktoken

if "qdrant_client" not in sys.modules:
    fake_qdrant = types.ModuleType("qdrant_client")
    fake_qdrant.AsyncQdrantClient = MagicMock()
    sys.modules["qdrant_client"] = fake_qdrant

if "qdrant_client.models" not in sys.modules:
    fake_qdrant_models = types.ModuleType("qdrant_client.models")
    fake_qdrant_models.Distance = MagicMock()
    fake_qdrant_models.PointIdsList = MagicMock()
    fake_qdrant_models.PointStruct = MagicMock()
    fake_qdrant_models.VectorParams = MagicMock()
    sys.modules["qdrant_client.models"] = fake_qdrant_models

if "rapidfuzz" not in sys.modules:
    fake_rapidfuzz = types.ModuleType("rapidfuzz")
    fake_rapidfuzz.fuzz = MagicMock()
    sys.modules["rapidfuzz"] = fake_rapidfuzz

if "python_multipart" not in sys.modules:
    fake_python_multipart = types.ModuleType("python_multipart")
    fake_python_multipart.__version__ = "0.0.20"
    sys.modules["python_multipart"] = fake_python_multipart

if "multipart" not in sys.modules:
    fake_multipart = types.ModuleType("multipart")
    fake_multipart.__version__ = "0.0.20"
    sys.modules["multipart"] = fake_multipart

if "multipart.multipart" not in sys.modules:
    fake_multipart_multipart = types.ModuleType("multipart.multipart")
    fake_multipart_multipart.parse_options_header = MagicMock()
    sys.modules["multipart.multipart"] = fake_multipart_multipart

import httpx
import pytest
from httpx import ASGITransport


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeRecord(dict):
    """Dict subclass that supports both dict[key] and .keys() like asyncpg.Record."""

    def keys(self):
        return super().keys()


class MockResponse:
    """Minimal HTTP response stub for mocking httpx calls."""

    def __init__(self, status_code: int, json_data: dict | None = None):
        self.status_code = status_code
        self._json = json_data or {}

    def json(self):
        return self._json


def _make_pool_and_conn():
    """Create a mock asyncpg Pool whose acquire() returns an async CM."""
    conn = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx
    return pool, conn


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
    from app.main import app, get_db_pool
    from jarvis_common import verify_api_key

    mock_pool, conn = _make_pool_and_conn()
    app.state.db_pool = mock_pool
    app.state.limiter.enabled = False
    mock_http = AsyncMock()
    app.state.http_client = mock_http

    app.dependency_overrides[get_db_pool] = lambda: mock_pool
    app.dependency_overrides[verify_api_key] = lambda: None
    yield app, conn, mock_http
    app.dependency_overrides.clear()


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
    from app.main import get_system_models
    request = _make_request(app.state.db_pool, mock_http)

    # Mock config DB fetch: return LLM config entries
    conn.fetch.return_value = [
        FakeRecord(key="llm.smart_model", value="mistral-nemo"),
        FakeRecord(key="llm.fast_model", value="qwen3.5:4b"),
        FakeRecord(key="llm.embed_model", value="nomic-embed-text"),
    ]

    async def mock_get_side_effect(url, **kwargs):
        if "/api/tags" in str(url):
            return MockResponse(
                200,
                {
                    "models": [
                        {
                            "name": "mistral-nemo",
                            "size": 4100000000,
                            "details": {
                                "parameter_size": "7B",
                                "quantization_level": "Q4_0",
                            },
                        }
                    ]
                },
            )
        elif "/api/ps" in str(url):
            return MockResponse(200, {"models": [{"name": "mistral-nemo"}]})
        return MockResponse(404)

    mock_http.get.side_effect = mock_get_side_effect

    body = await get_system_models(request)

    # Top-level keys
    assert body["status"] == "ok"
    assert "installed" in body
    assert "hardware" in body
    assert "current" in body
    assert body["issues"] == {}

    # Installed models
    assert len(body["installed"]) == 1
    assert body["installed"][0]["name"] == "mistral-nemo"
    assert body["installed"][0]["parameter_size"] == "7B"
    assert body["installed"][0]["quantization"] == "Q4_0"

    # Hardware info
    assert body["hardware"]["ollama_running"] == 1

    # Current config assignments (key stripped of 'llm.' prefix)
    assert body["current"]["smart_model"] == "mistral-nemo"
    assert body["current"]["fast_model"] == "qwen3.5:4b"
    assert body["current"]["embed_model"] == "nomic-embed-text"


@pytest.mark.asyncio
async def test_system_models_ollama_unreachable(_app):
    """GET /api/system/models returns empty installed list when Ollama is down."""
    app, conn, mock_http = _app
    from app.main import get_system_models
    request = _make_request(app.state.db_pool, mock_http)

    conn.fetch.return_value = []
    mock_http.get.side_effect = httpx.ConnectError("Connection refused")

    body = await get_system_models(request)
    assert body["status"] == "degraded"
    assert body["installed"] == []
    assert body["hardware"] == {}
    assert body["issues"] == {
        "installed": "Could not load installed Ollama models.",
        "runtime": "Could not load Ollama runtime status.",
    }


@pytest.mark.asyncio
async def test_system_models_no_config(_app):
    """GET /api/system/models returns empty current dict when no config exists."""
    app, conn, mock_http = _app
    from app.main import get_system_models
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
    assert body["status"] == "ok"
    assert body["current"] == {}
    assert len(body["installed"]) == 1
    assert body["hardware"]["ollama_running"] == 0
    assert body["issues"] == {}


@pytest.mark.asyncio
async def test_system_models_db_failure_still_returns_ollama_data(_app):
    """GET /api/system/models degrades when config loading fails but still returns Ollama data."""
    app, conn, mock_http = _app
    from app.main import get_system_models
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
    assert body["status"] == "degraded"
    assert body["current"] == {}
    assert len(body["installed"]) == 1
    assert body["hardware"]["ollama_running"] == 1
    assert body["issues"] == {
        "current": "Could not load current model assignments.",
    }


@pytest.mark.asyncio
async def test_system_models_runtime_probe_failure_keeps_installed_models(_app):
    """GET /api/system/models keeps installed models even when runtime probe fails."""
    app, conn, mock_http = _app
    from app.main import get_system_models
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
    assert body["status"] == "degraded"
    assert len(body["installed"]) == 1
    assert body["hardware"] == {}
    assert body["issues"] == {
        "runtime": "Could not load Ollama runtime status.",
    }
