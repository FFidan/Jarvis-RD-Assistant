"""Tests for GET /api/system/capabilities (B6 library probe)."""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest
from httpx import ASGITransport
from jarvis_common.testing import make_pool_and_conn


def _make_pool():
    pool, _ = make_pool_and_conn()
    return pool


@pytest.fixture()
def _app(monkeypatch):
    from jarvis_common import verify_api_key
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app

    pool = _make_pool()
    app.state.db_pool = pool
    app.state.limiter.enabled = False

    app.dependency_overrides[get_db_pool] = lambda: pool
    app.dependency_overrides[verify_api_key] = lambda: None
    yield app
    app.dependency_overrides.clear()
    app.state.limiter.enabled = True


@pytest.mark.asyncio
async def test_capabilities_returns_both_boolean_keys(_app):
    """Response has exactly {networkx, scikit_learn} with boolean values."""
    async with httpx.AsyncClient(
        transport=ASGITransport(app=_app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/system/capabilities")

    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"networkx", "scikit_learn"}
    assert isinstance(body["networkx"], bool)
    assert isinstance(body["scikit_learn"], bool)


@pytest.mark.asyncio
async def test_capabilities_true_when_specs_found(_app):
    """When both find_spec calls return a non-None spec, both flags are True."""
    fake_spec = object()  # any truthy non-None value
    with patch("importlib.util.find_spec", return_value=fake_spec):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=_app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/system/capabilities")

    assert resp.status_code == 200
    body = resp.json()
    assert body["networkx"] is True
    assert body["scikit_learn"] is True


@pytest.mark.asyncio
async def test_capabilities_false_when_specs_missing(_app):
    """When find_spec returns None (lib not installed), both flags are False."""
    with patch("importlib.util.find_spec", return_value=None):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=_app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/system/capabilities")

    assert resp.status_code == 200
    body = resp.json()
    assert body["networkx"] is False
    assert body["scikit_learn"] is False


@pytest.mark.asyncio
async def test_capabilities_requires_auth(monkeypatch):
    """Without a valid API key the endpoint must return 401/403."""
    monkeypatch.setenv("JARVIS_API_KEY", "secret-key-value-1234567890")

    from jarvis_common.auth import refresh_api_key_cache
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app

    refresh_api_key_cache()
    pool = _make_pool()
    app.state.db_pool = pool
    old_limiter = app.state.limiter.enabled
    app.state.limiter.enabled = False
    app.dependency_overrides[get_db_pool] = lambda: pool

    try:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/system/capabilities")
    finally:
        app.dependency_overrides.clear()
        app.state.limiter.enabled = old_limiter
        monkeypatch.delenv("JARVIS_API_KEY", raising=False)
        refresh_api_key_cache()

    assert resp.status_code in (401, 403)
