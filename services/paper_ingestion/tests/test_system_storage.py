"""Tests for GET /api/system/storage (admin disk-usage snapshot).

Covers the response shape, per-backend totals, the pressure flag, and that an
unreachable backend degrades to a null/error section instead of a 500 —
admin-gate coverage lives in test_system_admin_gates.py.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from httpx import ASGITransport
from jarvis_common.testing_contract_apps import PITestAppOptions, patch_pi_test_app

from tests.conftest import _make_pool_and_conn


@pytest.fixture()
def _app():
    from jarvis_common import verify_api_key
    from paper_ingestion.deps import get_db_pool, limiter
    from paper_ingestion.main import app

    pool, conn = _make_pool_and_conn()

    tags_resp = MagicMock()
    tags_resp.status_code = 200
    tags_resp.json.return_value = {"models": [{"name": "qwen3:8b", "size": 5_000_000_000}]}
    http = MagicMock()
    http.get = AsyncMock(return_value=tags_resp)

    with patch_pi_test_app(
        pool,
        app=app,
        get_db_pool=get_db_pool,
        limiter=limiter,
        options=PITestAppOptions(
            remove_owner_override=False,
            override_db_dependency=True,
            disable_limiter=True,
            state_overrides={"http_client": http},
            # The shape test asserts the degraded "no Qdrant wired" section, so
            # the client must be absent no matter what an earlier test left on
            # the app singleton; tests that need one set it themselves and the
            # helper removes it again on exit.
            state_absent=("qdrant_client",),
            dependency_overrides={verify_api_key: lambda: None},
        ),
    ):
        yield app, conn


async def _get_storage(app) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.get("/api/system/storage")


@pytest.mark.asyncio
async def test_storage_shape_and_totals(_app, tmp_path, monkeypatch):
    """Response shape + Ollama/Postgres totals; no qdrant_client wired → degrades."""
    app, conn = _app
    conn.fetchval = AsyncMock(return_value=123_456)
    monkeypatch.setattr("paper_ingestion.routers.system._HF_CACHE_DIR", tmp_path)

    resp = await _get_storage(app)

    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {
        "ollama_models",
        "postgres",
        "qdrant",
        "qdrant_collections",
        "hf_cache",
        "pressure",
    }
    assert body["ollama_models"] == {"bytes_used": 5_000_000_000, "error": None}
    assert body["postgres"] == {"bytes_used": 123_456, "error": None}
    assert body["hf_cache"] == {"bytes_used": 0, "error": None}  # tmp_path exists but empty
    assert body["qdrant"] == {"bytes_used": None, "error": "Qdrant client not available"}
    assert body["qdrant_collections"] == []
    assert isinstance(body["pressure"], bool)


@pytest.mark.asyncio
async def test_storage_hf_cache_measures_real_files(_app, tmp_path, monkeypatch):
    app, conn = _app
    conn.fetchval = AsyncMock(return_value=0)
    (tmp_path / "a.bin").write_bytes(b"x" * 1000)
    (tmp_path / "b.bin").write_bytes(b"y" * 2000)
    monkeypatch.setattr("paper_ingestion.routers.system._HF_CACHE_DIR", tmp_path)

    resp = await _get_storage(app)

    assert resp.status_code == 200
    assert resp.json()["hf_cache"]["bytes_used"] == 3000


@pytest.mark.asyncio
async def test_storage_pressure_true_when_free_space_below_floor(_app, tmp_path, monkeypatch):
    app, conn = _app
    conn.fetchval = AsyncMock(return_value=0)
    monkeypatch.setattr("paper_ingestion.routers.system._HF_CACHE_DIR", tmp_path)
    monkeypatch.setattr(
        "paper_ingestion.routers.system.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=1_000_000_000),  # 1 GB, below the 6 GB floor
    )

    resp = await _get_storage(app)

    assert resp.json()["pressure"] is True


@pytest.mark.asyncio
async def test_storage_pressure_false_when_free_space_ample(_app, tmp_path, monkeypatch):
    app, conn = _app
    conn.fetchval = AsyncMock(return_value=0)
    monkeypatch.setattr("paper_ingestion.routers.system._HF_CACHE_DIR", tmp_path)
    monkeypatch.setattr(
        "paper_ingestion.routers.system.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=99_000_000_000),  # 99 GB
    )

    resp = await _get_storage(app)

    assert resp.json()["pressure"] is False


@pytest.mark.asyncio
async def test_storage_postgres_unreachable_degrades_without_500(_app, tmp_path, monkeypatch):
    app, conn = _app
    conn.fetchval = AsyncMock(side_effect=RuntimeError("connection refused"))
    monkeypatch.setattr("paper_ingestion.routers.system._HF_CACHE_DIR", tmp_path)

    resp = await _get_storage(app)

    assert resp.status_code == 200
    assert resp.json()["postgres"] == {"bytes_used": None, "error": "RuntimeError"}


@pytest.mark.asyncio
async def test_storage_ollama_unreachable_degrades_without_500(_app, tmp_path, monkeypatch):
    app, conn = _app
    conn.fetchval = AsyncMock(return_value=0)
    app.state.http_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
    monkeypatch.setattr("paper_ingestion.routers.system._HF_CACHE_DIR", tmp_path)

    resp = await _get_storage(app)

    assert resp.status_code == 200
    assert resp.json()["ollama_models"] == {
        "bytes_used": None,
        "error": "Could not load installed Ollama models.",
    }


@pytest.mark.asyncio
async def test_storage_qdrant_collections_return_point_counts(_app, tmp_path, monkeypatch):
    app, conn = _app
    conn.fetchval = AsyncMock(return_value=0)
    monkeypatch.setattr("paper_ingestion.routers.system._HF_CACHE_DIR", tmp_path)

    papers = SimpleNamespace(name="papers")
    kg_entities = SimpleNamespace(name="kg_entities")
    collections_resp = SimpleNamespace(collections=[papers, kg_entities])

    qdrant = MagicMock()
    qdrant.get_collections = AsyncMock(return_value=collections_resp)
    qdrant.get_collection = AsyncMock(
        side_effect=[SimpleNamespace(points_count=1200), SimpleNamespace(points_count=340)]
    )
    app.state.qdrant_client = qdrant

    resp = await _get_storage(app)

    assert resp.status_code == 200
    body = resp.json()
    assert body["qdrant"] == {"bytes_used": None, "error": None}
    assert body["qdrant_collections"] == [
        {"name": "papers", "points_count": 1200},
        {"name": "kg_entities", "points_count": 340},
    ]


@pytest.mark.asyncio
async def test_storage_qdrant_unreachable_degrades_without_500(_app, tmp_path, monkeypatch):
    app, conn = _app
    conn.fetchval = AsyncMock(return_value=0)
    monkeypatch.setattr("paper_ingestion.routers.system._HF_CACHE_DIR", tmp_path)

    qdrant = MagicMock()
    qdrant.get_collections = AsyncMock(side_effect=RuntimeError("unreachable"))
    app.state.qdrant_client = qdrant

    resp = await _get_storage(app)

    assert resp.status_code == 200
    body = resp.json()
    assert body["qdrant"] == {"bytes_used": None, "error": "RuntimeError"}
    assert body["qdrant_collections"] == []
