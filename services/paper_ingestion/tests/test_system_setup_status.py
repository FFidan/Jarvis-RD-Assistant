"""Tests for GET /api/system/setup-status (A1 setup wizard backend)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx  # noqa: E402
import pytest  # noqa: E402
from httpx import ASGITransport  # noqa: E402


class FakeRecord(dict):
    def keys(self):
        return super().keys()


def _make_pool_and_conn():
    conn = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx
    return pool, conn


def _user_config_rows(
    *,
    setup_completed=False,
    owner_chat_id=None,
) -> list[FakeRecord]:
    rows: list[FakeRecord] = [
        FakeRecord(key="setup.completed", value=setup_completed),
        FakeRecord(key="telegram.owner_chat_id", value=owner_chat_id),
    ]
    return rows


@pytest.fixture()
def _app(monkeypatch):
    # Ensure TELEGRAM_BOT_TOKEN is deterministically absent by default.
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://ollama-mock:11434")

    from jarvis_common import verify_api_key
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app

    mock_pool, conn = _make_pool_and_conn()
    app.state.db_pool = mock_pool
    app.state.limiter.enabled = False

    app.dependency_overrides[get_db_pool] = lambda: mock_pool
    app.dependency_overrides[verify_api_key] = lambda: None
    yield app, conn
    app.dependency_overrides.clear()
    app.state.limiter.enabled = True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _patch_probe(monkeypatch, *, models_ready: bool, downloading=None):
    """Replace ``_probe_ollama`` so setup-status tests don't touch the network."""

    async def _fake_probe():
        return models_ready, list(downloading or [])

    monkeypatch.setattr("paper_ingestion.routers.system._probe_ollama", _fake_probe)


def _install_user_config(
    conn,
    *,
    setup_completed=False,
    owner_chat_id=None,
    topics_count: int = 0,
):
    conn.fetch.return_value = _user_config_rows(
        setup_completed=setup_completed,
        owner_chat_id=owner_chat_id,
    )
    conn.fetchrow.return_value = FakeRecord(n=topics_count)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_setup_status_reads_user_config(_app, monkeypatch):
    app, conn = _app
    _install_user_config(conn, setup_completed=True, owner_chat_id=42, topics_count=3)
    _patch_probe(monkeypatch, models_ready=False)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/system/setup-status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["setup_completed"] is True
    assert body["telegram_paired"] is True
    assert body["topics_count"] == 3


@pytest.mark.asyncio
async def test_setup_status_telegram_configured_reads_env(_app, monkeypatch):
    app, conn = _app
    _install_user_config(conn)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "dummy:secret")
    _patch_probe(monkeypatch, models_ready=False)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/system/setup-status")

    assert resp.status_code == 200
    assert resp.json()["telegram_configured"] is True


@pytest.mark.asyncio
async def test_setup_status_telegram_configured_false_when_env_missing(_app, monkeypatch):
    app, conn = _app
    _install_user_config(conn)
    _patch_probe(monkeypatch, models_ready=False)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/system/setup-status")

    assert resp.status_code == 200
    assert resp.json()["telegram_configured"] is False


@pytest.mark.asyncio
async def test_setup_status_models_ready_true_when_ollama_ok(_app, monkeypatch):
    app, conn = _app
    _install_user_config(conn)
    _patch_probe(monkeypatch, models_ready=True)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/system/setup-status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["models_ready"] is True
    assert body["models_downloading"] == []


@pytest.mark.asyncio
async def test_setup_status_models_ready_false_on_ollama_error(_app, monkeypatch):
    app, conn = _app
    _install_user_config(conn)
    _patch_probe(monkeypatch, models_ready=False)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/system/setup-status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["models_ready"] is False
    assert body["models_downloading"] == []


@pytest.mark.asyncio
async def test_setup_status_models_ready_false_when_expected_missing(_app, monkeypatch):
    app, conn = _app
    _install_user_config(conn)
    _patch_probe(monkeypatch, models_ready=False)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/system/setup-status")

    assert resp.status_code == 200
    assert resp.json()["models_ready"] is False


@pytest.mark.asyncio
async def test_setup_status_topics_count_from_db(_app, monkeypatch):
    app, conn = _app
    _install_user_config(conn, topics_count=17)
    _patch_probe(monkeypatch, models_ready=False)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/system/setup-status")

    assert resp.status_code == 200
    assert resp.json()["topics_count"] == 17


@pytest.mark.asyncio
async def test_setup_status_telegram_paired_false_when_null(_app, monkeypatch):
    app, conn = _app
    _install_user_config(conn, owner_chat_id=None)
    _patch_probe(monkeypatch, models_ready=False)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/system/setup-status")

    assert resp.status_code == 200
    assert resp.json()["telegram_paired"] is False


# ---------------------------------------------------------------------------
# Direct unit tests for _probe_ollama / _models_match helpers
# ---------------------------------------------------------------------------


def test_models_match_true_when_all_prefixes_present():
    from paper_ingestion.routers.system import _models_match

    assert _models_match(["mistral-nemo:latest", "qwen3.5:4b", "nomic-embed-text:latest"]) is True


def test_models_match_false_when_missing_prefix():
    from paper_ingestion.routers.system import _models_match

    assert _models_match(["mistral-nemo:latest"]) is False


def test_models_match_false_on_empty():
    from paper_ingestion.routers.system import _models_match

    assert _models_match([]) is False


@pytest.mark.asyncio
async def test_probe_ollama_returns_false_when_unreachable(monkeypatch):
    """_probe_ollama must not raise when Ollama is down."""
    from paper_ingestion.routers import system as system_module

    class _BoomClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, *_a, **_k):
            raise RuntimeError("connection refused")

    monkeypatch.setattr(system_module.httpx, "AsyncClient", lambda *a, **k: _BoomClient())

    ready, downloading = await system_module._probe_ollama()
    assert ready is False
    assert downloading == []
