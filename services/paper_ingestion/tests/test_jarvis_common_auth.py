"""Direct tests for shared authentication helpers."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from jarvis_common.auth import (
    refresh_api_key_cache,
    validate_production_config,
    verify_api_key,
)


def _request(path: str):
    """Create a minimal request stub."""
    return SimpleNamespace(url=SimpleNamespace(path=path))


@pytest.mark.asyncio
async def test_verify_api_key_allows_health_without_configuration(monkeypatch):
    """Health checks bypass authentication even when no key is configured."""
    monkeypatch.delenv("JARVIS_API_KEY", raising=False)
    monkeypatch.delenv("DEV_MODE", raising=False)

    await verify_api_key(_request("/health"), api_key=None)


@pytest.mark.asyncio
async def test_verify_api_key_allows_dev_mode_without_key(monkeypatch):
    """DEV_MODE bypasses authentication when no API key is configured."""
    monkeypatch.delenv("JARVIS_API_KEY", raising=False)
    monkeypatch.setenv("DEV_MODE", "true")
    refresh_api_key_cache()

    await verify_api_key(_request("/api/papers"), api_key=None)


@pytest.mark.asyncio
async def test_verify_api_key_rejects_missing_config_in_non_dev(monkeypatch):
    """Missing configuration raises 401 outside DEV_MODE."""
    monkeypatch.delenv("JARVIS_API_KEY", raising=False)
    monkeypatch.setenv("DEV_MODE", "false")
    # Refresh the cache so any previously-set key (from other tests) is cleared.
    refresh_api_key_cache()

    with pytest.raises(HTTPException, match="API key not configured") as exc_info:
        await verify_api_key(_request("/api/papers"), api_key=None)

    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_verify_api_key_rejects_invalid_key(monkeypatch):
    """Mismatched keys raise 403."""
    monkeypatch.setenv("JARVIS_API_KEY", "x" * 32)
    monkeypatch.setenv("DEV_MODE", "false")
    refresh_api_key_cache()

    with pytest.raises(HTTPException, match="Invalid or missing API key") as exc_info:
        await verify_api_key(_request("/api/papers"), api_key="wrong-key")

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_verify_api_key_accepts_valid_key(monkeypatch):
    """Matching API keys should allow authenticated requests through."""
    monkeypatch.setenv("JARVIS_API_KEY", "x" * 32)
    monkeypatch.setenv("DEV_MODE", "false")
    refresh_api_key_cache()

    await verify_api_key(_request("/api/papers"), api_key="x" * 32)


def test_validate_production_config_rejects_dev_mode_in_production(monkeypatch):
    """Production mode may not run with DEV_MODE=true."""
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("DEV_MODE", "true")
    monkeypatch.setenv("JARVIS_API_KEY", "x" * 32)

    with pytest.raises(RuntimeError, match="DEV_MODE=true"):
        validate_production_config()


def test_validate_production_config_rejects_short_key(monkeypatch):
    """Non-dev mode requires a real 32+ character API key."""
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("DEV_MODE", "false")
    monkeypatch.setenv("JARVIS_API_KEY", "short")

    with pytest.raises(RuntimeError, match="at least 32 characters"):
        validate_production_config()


def test_validate_production_config_rejects_default_sentinel(monkeypatch):
    """The documented placeholder value must not pass startup validation."""
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("DEV_MODE", "false")
    monkeypatch.setenv("JARVIS_API_KEY", "CHANGE_ME_REQUIRED")

    with pytest.raises(RuntimeError, match="not empty or default sentinel"):
        validate_production_config()


def test_validate_production_config_accepts_long_key(monkeypatch):
    """A long non-default API key passes validation."""
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("DEV_MODE", "false")
    monkeypatch.setenv("JARVIS_API_KEY", "x" * 32)

    validate_production_config()


def test_validate_production_config_accepts_dedicated_model_hmac_key(monkeypatch):
    """In production, ``JARVIS_MODEL_HMAC_KEY`` satisfies the H14 gate.

    Even when set, ``JARVIS_API_KEY`` is still required (HTTP auth) — so this
    test ensures both gates are independently satisfied.
    """
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("DEV_MODE", "false")
    monkeypatch.setenv("JARVIS_API_KEY", "x" * 32)
    monkeypatch.setenv("JARVIS_MODEL_HMAC_KEY", "y" * 32)

    validate_production_config()
