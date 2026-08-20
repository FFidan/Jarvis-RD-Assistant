"""Tests for GET /api/system/effective-config (M1.5 resolved-vs-default dump)."""

from __future__ import annotations

import httpx
import pytest
from httpx import ASGITransport
from jarvis_common.testing import make_pool_and_conn
from jarvis_common.testing_contract_apps import PITestAppOptions, patch_pi_test_app


def _make_pool(fetch_return=None):
    pool, _ = make_pool_and_conn(fetch_return=fetch_return or [])
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
            remove_identity_overrides=False,
            override_db_dependency=True,
            disable_limiter=True,
            dependency_overrides={verify_api_key: lambda: None},
        ),
    ):
        yield app


@pytest.mark.asyncio
async def test_effective_config_default_snapshot(_app):
    """No DB overrides → effective equals code_default for each role + enforcement block."""
    async with httpx.AsyncClient(
        transport=ASGITransport(app=_app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/system/effective-config")

    assert resp.status_code == 200
    body = resp.json()
    assert set(body["roles"]) == {"smart", "fast", "embed", "pulse_stage2"}

    pulse = body["roles"]["pulse_stage2"]
    assert pulse["code_default"] == "smart"
    assert pulse["effective"] == "smart"
    assert pulse["transport_prefix"] == "ollama_chat/"
    assert body["roles"]["embed"]["transport_prefix"] == "ollama/"

    assert body["instructor_mode"] == "JSON_SCHEMA"
    assert body["structured_output_enforced"] is True
    assert body["drop_params"] is True
    assert body["think_disabled"] == {"smart": True, "fast": True}


@pytest.mark.asyncio
async def test_effective_config_db_override_visible(monkeypatch):
    """A stored llm.smart_model override surfaces as effective != code_default."""
    from jarvis_common import verify_api_key
    from paper_ingestion.deps import get_db_pool, limiter
    from paper_ingestion.main import app

    pool = _make_pool(fetch_return=[{"key": "llm.smart_model", "value": '"qwen3:14b"'}])
    with patch_pi_test_app(
        pool,
        app=app,
        get_db_pool=get_db_pool,
        limiter=limiter,
        options=PITestAppOptions(
            remove_identity_overrides=False,
            override_db_dependency=True,
            disable_limiter=True,
            dependency_overrides={verify_api_key: lambda: None},
        ),
    ):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/system/effective-config")

    assert resp.status_code == 200
    smart = resp.json()["roles"]["smart"]
    assert smart["code_default"] == "qwen3:8b"
    assert smart["effective"] == "qwen3:14b"


@pytest.mark.asyncio
async def test_effective_config_pulse_override_visible(_app, monkeypatch):
    """A PULSE_STAGE2_MODEL=fast env override surfaces as effective != code_default."""
    monkeypatch.setenv("PULSE_STAGE2_MODEL", "fast")
    async with httpx.AsyncClient(
        transport=ASGITransport(app=_app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/system/effective-config")

    assert resp.status_code == 200
    pulse = resp.json()["roles"]["pulse_stage2"]
    assert pulse["code_default"] == "smart"
    assert pulse["effective"] == "fast"


@pytest.mark.asyncio
async def test_effective_config_requires_auth(monkeypatch):
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
                remove_identity_overrides=False,
                override_db_dependency=True,
                disable_limiter=True,
            ),
        ):
            async with httpx.AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/api/system/effective-config")
    finally:
        monkeypatch.delenv("JARVIS_API_KEY", raising=False)
        refresh_api_key_cache()

    assert resp.status_code in (401, 403)


def test_role_code_defaults_mirror_canonical_fallbacks():
    """The dump's hardcoded smart/fast defaults must track main.py's canonical source.

    M1.5 mirrors the role defaults locally to avoid importing the FastAPI entrypoint at
    runtime. This test pins the mirror to ``_LITELLM_ROLE_FALLBACKS`` so a default change
    that is not reflected here fails loudly — a drift-detector must not silently drift.
    Both structures must also agree with the single-source constants so future
    per-file edits are caught immediately.
    """
    from paper_ingestion.constants import FAST_MODEL_DEFAULT, SMART_MODEL_DEFAULT
    from paper_ingestion.litellm_reconciler import _LITELLM_ROLE_FALLBACKS
    from paper_ingestion.routers.system import _ROLE_CODE_DEFAULTS

    for role, default in _ROLE_CODE_DEFAULTS.items():
        assert default == _LITELLM_ROLE_FALLBACKS[f"llm.{role}_model"][1]

    assert _ROLE_CODE_DEFAULTS["smart"] == SMART_MODEL_DEFAULT
    assert _ROLE_CODE_DEFAULTS["fast"] == FAST_MODEL_DEFAULT
    assert _LITELLM_ROLE_FALLBACKS["llm.smart_model"][1] == SMART_MODEL_DEFAULT
    assert _LITELLM_ROLE_FALLBACKS["llm.fast_model"][1] == FAST_MODEL_DEFAULT
