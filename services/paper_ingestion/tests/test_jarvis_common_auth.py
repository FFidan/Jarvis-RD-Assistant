"""Direct tests for shared authentication helpers."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from jarvis_common.auth import (
    refresh_api_key_cache,
    validate_production_config,
    verify_api_key,
)


def _request(path: str):
    """Create a minimal request stub."""
    return SimpleNamespace(url=SimpleNamespace(path=path))


@pytest.mark.asyncio
async def test_verify_api_key_allows_dev_mode_without_key(monkeypatch):
    """DEV_MODE bypasses authentication when no API key is configured."""
    monkeypatch.delenv("JARVIS_API_KEY", raising=False)
    monkeypatch.setenv("DEV_MODE", "true")
    refresh_api_key_cache()

    await verify_api_key(_request("/api/papers"), api_key=None)


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
    H-2: production also requires SMTP to be fully configured (DEV_SMTP_LOG_ONLY
    is forbidden in production, so real SMTP vars are the only valid path).
    """
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("DEV_MODE", "false")
    monkeypatch.setenv("JARVIS_API_KEY", "x" * 32)
    monkeypatch.setenv("JARVIS_MODEL_HMAC_KEY", "y" * 32)
    monkeypatch.setenv("JARVIS_CONFIG_KEY", "z" * 44)  # Fernet keys are 44 chars
    # H-2: SMTP must be fully configured in production.
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_FROM", "jarvis@example.com")
    # SEC-A/SEC-B: production also requires a strong LiteLLM master key, a
    # strong Postgres password, and an explicit APP_BASE_URL.
    monkeypatch.setenv("LITELLM_MASTER_KEY", "sk-" + "p" * 40)
    monkeypatch.setenv("POSTGRES_PASSWORD", "q" * 24)
    monkeypatch.setenv("APP_BASE_URL", "https://jarvis.example.com")

    validate_production_config()


def test_validate_production_config_requires_config_key(monkeypatch):
    """Production without JARVIS_CONFIG_KEY must fail at boot (DOM-E-08)."""
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("DEV_MODE", "false")
    monkeypatch.setenv("JARVIS_API_KEY", "x" * 32)
    monkeypatch.setenv("JARVIS_MODEL_HMAC_KEY", "y" * 32)
    monkeypatch.delenv("JARVIS_CONFIG_KEY", raising=False)
    monkeypatch.delenv("JARVIS_CONFIG_KEY_FILE", raising=False)

    with pytest.raises(RuntimeError, match="JARVIS_CONFIG_KEY"):
        validate_production_config()


def test_validate_production_config_requires_hmac_key(monkeypatch):
    """Production without JARVIS_MODEL_HMAC_KEY must fail at boot (M-07).

    The derivation-from-JARVIS_API_KEY fallback was removed in production so
    a stolen bearer cannot also forge pulse model blobs.
    """
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("DEV_MODE", "false")
    monkeypatch.setenv("JARVIS_API_KEY", "x" * 32)
    monkeypatch.setenv("JARVIS_CONFIG_KEY", "z" * 44)
    monkeypatch.delenv("JARVIS_MODEL_HMAC_KEY", raising=False)

    with pytest.raises(RuntimeError, match="JARVIS_MODEL_HMAC_KEY"):
        validate_production_config()


def test_validate_production_config_rejects_short_hmac_key(monkeypatch):
    """``JARVIS_MODEL_HMAC_KEY`` must be at least 32 characters in production (M-07)."""
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("DEV_MODE", "false")
    monkeypatch.setenv("JARVIS_API_KEY", "x" * 32)
    monkeypatch.setenv("JARVIS_CONFIG_KEY", "z" * 44)
    monkeypatch.setenv("JARVIS_MODEL_HMAC_KEY", "y" * 16)

    with pytest.raises(RuntimeError, match="at least 32 characters"):
        validate_production_config()


def test_validate_production_config_config_key_not_required_outside_production(
    monkeypatch,
):
    """JARVIS_CONFIG_KEY is only required in production; development should pass without it."""
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("DEV_MODE", "false")
    monkeypatch.setenv("JARVIS_API_KEY", "x" * 32)
    monkeypatch.delenv("JARVIS_CONFIG_KEY", raising=False)
    monkeypatch.delenv("JARVIS_CONFIG_KEY_FILE", raising=False)

    validate_production_config()  # must not raise


def test_validate_production_config_multi_user_nonprod_requires_hmac_key(monkeypatch):
    """SEC-4: a multi-user non-prod boot must require JARVIS_MODEL_HMAC_KEY.

    The derivation-from-JARVIS_API_KEY fallback is refused on any multi-user
    deployment (``JARVIS_SETUP_MODE != single``), not only in production — so a
    stolen bearer cannot also forge pulse model blobs on an internal multi-user
    box (e.g. REDACTED-HOST).
    """
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("DEV_MODE", "false")
    monkeypatch.setenv("JARVIS_API_KEY", "x" * 32)
    monkeypatch.setenv("JARVIS_SETUP_MODE", "multi")
    monkeypatch.delenv("JARVIS_MODEL_HMAC_KEY", raising=False)
    monkeypatch.delenv("JARVIS_MODEL_HMAC_KEY_FILE", raising=False)

    with pytest.raises(RuntimeError, match="JARVIS_MODEL_HMAC_KEY"):
        validate_production_config()


def test_validate_production_config_multi_user_nonprod_accepts_hmac_key(monkeypatch):
    """SEC-4: a multi-user non-prod boot passes once JARVIS_MODEL_HMAC_KEY is set.

    Multi-user does not pull in the production-only gates (CONFIG_KEY, SMTP,
    LiteLLM, Postgres, APP_BASE_URL) — only the broadened HMAC requirement.
    """
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("DEV_MODE", "false")
    monkeypatch.setenv("JARVIS_API_KEY", "x" * 32)
    monkeypatch.setenv("JARVIS_SETUP_MODE", "multi")
    monkeypatch.setenv("JARVIS_MODEL_HMAC_KEY", "y" * 32)

    validate_production_config()  # must not raise


def test_validate_production_config_multi_user_nonprod_rejects_short_hmac_key(monkeypatch):
    """SEC-4: the broadened gate also enforces the 32-char minimum."""
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("DEV_MODE", "false")
    monkeypatch.setenv("JARVIS_API_KEY", "x" * 32)
    monkeypatch.setenv("JARVIS_SETUP_MODE", "multi")
    monkeypatch.setenv("JARVIS_MODEL_HMAC_KEY", "y" * 16)

    with pytest.raises(RuntimeError, match="at least 32 characters"):
        validate_production_config()


def test_validate_production_config_single_user_dev_does_not_require_hmac_key(monkeypatch):
    """SEC-4 non-regression: single-user dev boot stays unchanged (no HMAC key needed)."""
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("DEV_MODE", "false")
    monkeypatch.setenv("JARVIS_API_KEY", "x" * 32)
    monkeypatch.setenv("JARVIS_SETUP_MODE", "single")
    monkeypatch.delenv("JARVIS_MODEL_HMAC_KEY", raising=False)
    monkeypatch.delenv("JARVIS_MODEL_HMAC_KEY_FILE", raising=False)

    validate_production_config()  # must not raise
