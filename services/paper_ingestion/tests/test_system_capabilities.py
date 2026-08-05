"""Tests for GET /api/system/capabilities (B6 library probe)."""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest
from httpx import ASGITransport
from jarvis_common.testing import make_pool_and_conn
from jarvis_common.testing_contract_apps import PITestAppOptions, patch_pi_test_app


def _make_pool():
    pool, _ = make_pool_and_conn()
    return pool


@pytest.fixture()
def _app(monkeypatch):
    from jarvis_common import verify_api_key
    from paper_ingestion.deps import get_db_pool, limiter
    from paper_ingestion.main import app

    pool = _make_pool()
    with patch_pi_test_app(
        pool,
        app=app,
        get_db_pool=get_db_pool,
        limiter=limiter,
        options=PITestAppOptions(
            remove_owner_override=False,
            override_db_dependency=True,
            disable_limiter=True,
            dependency_overrides={verify_api_key: lambda: None},
        ),
    ):
        yield app


@pytest.mark.asyncio
async def test_capabilities_returns_both_boolean_keys(_app):
    """Response has exactly {networkx, scikit_learn} with boolean values."""
    async with httpx.AsyncClient(
        transport=ASGITransport(app=_app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/system/capabilities")

    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"networkx", "scikit_learn", "structured_output_enforced"}
    assert isinstance(body["networkx"], bool)
    assert isinstance(body["scikit_learn"], bool)
    assert isinstance(body["structured_output_enforced"], bool)


@pytest.mark.asyncio
async def test_capabilities_structured_output_enforced_true_on_default(_app):
    """The shipped default instructor mode is grammar-enforcing, so the flag is True."""
    async with httpx.AsyncClient(
        transport=ASGITransport(app=_app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/system/capabilities")

    assert resp.status_code == 200
    assert resp.json()["structured_output_enforced"] is True


def test_grammar_enforcing_modes_reject_prompt_only_mode():
    """A non-grammar mode name is not in the enforcing set (a silent revert flips False)."""
    from paper_ingestion.routers.system import _GRAMMAR_ENFORCING_MODES

    assert "JSON_SCHEMA" in _GRAMMAR_ENFORCING_MODES
    assert "JSON" not in _GRAMMAR_ENFORCING_MODES


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
    from paper_ingestion.deps import get_db_pool, limiter
    from paper_ingestion.main import app

    refresh_api_key_cache()
    pool = _make_pool()
    try:
        with patch_pi_test_app(
            pool,
            app=app,
            get_db_pool=get_db_pool,
            limiter=limiter,
            options=PITestAppOptions(
                remove_owner_override=False,
                override_db_dependency=True,
                disable_limiter=True,
            ),
        ):
            async with httpx.AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/api/system/capabilities")
    finally:
        monkeypatch.delenv("JARVIS_API_KEY", raising=False)
        refresh_api_key_cache()

    assert resp.status_code in (401, 403)
