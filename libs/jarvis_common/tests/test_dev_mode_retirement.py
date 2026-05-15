"""Tests for WS-DEV-MODE-RETIREMENT: granular dev flags + meta-flag promotion."""

from __future__ import annotations

import asyncio

import jarvis_common.auth as auth_mod
import pytest
from jarvis_common.auth import validate_production_config
from jarvis_common.settings import get_core_settings
from starlette.requests import Request

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(path: str = "/api/papers") -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "query_string": b"",
        "headers": [],
    }
    return Request(scope)


def _clear_env(monkeypatch) -> None:
    """Remove all dev flag env vars so tests start from a clean state."""
    for var in (
        "DEV_MODE",
        "DEV_AUTH_BYPASS",
        "DEV_ERROR_DETAIL",
        "DEV_CORS_OPEN",
        "DEV_SMTP_LOG_ONLY",
        "DEV_CRYPTO_RELAXED",
        "ENVIRONMENT",
        "JARVIS_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# (a) DEV_AUTH_BYPASS=true without DEV_MODE bypasses verify_api_key
# ---------------------------------------------------------------------------


class TestDevAuthBypassStandalone:
    def test_dev_auth_bypass_allows_without_key(self, monkeypatch) -> None:
        """DEV_AUTH_BYPASS=true with DEV_MODE unset still bypasses verify_api_key
        when no API key is configured."""
        _clear_env(monkeypatch)
        monkeypatch.setenv("DEV_AUTH_BYPASS", "true")

        monkeypatch.setattr(auth_mod, "_CACHED_API_KEY", None)
        request = _make_request()
        # Should not raise
        asyncio.get_event_loop().run_until_complete(auth_mod.verify_api_key(request, None))

    def test_dev_auth_bypass_false_raises_401_without_key(self, monkeypatch) -> None:
        """With no key and no bypass, verify_api_key raises 401."""
        from fastapi import HTTPException

        _clear_env(monkeypatch)
        monkeypatch.setenv("DEV_AUTH_BYPASS", "false")

        monkeypatch.setattr(auth_mod, "_CACHED_API_KEY", None)
        request = _make_request()
        with pytest.raises(HTTPException) as exc_info:
            asyncio.get_event_loop().run_until_complete(auth_mod.verify_api_key(request, None))
        assert exc_info.value.status_code == 401

    # (b) DEV_AUTH_BYPASS=true + ENVIRONMENT=production crashes validate_production_config
    def test_dev_auth_bypass_crashes_in_production(self, monkeypatch) -> None:
        """validate_production_config raises when DEV_AUTH_BYPASS=true in production."""
        _clear_env(monkeypatch)
        monkeypatch.setenv("ENVIRONMENT", "production")
        monkeypatch.setenv("DEV_AUTH_BYPASS", "true")
        # Provide minimal valid prod secrets so the API-key check doesn't fire first
        monkeypatch.setenv("JARVIS_API_KEY", "a" * 32)

        with pytest.raises(RuntimeError, match="dev_auth_bypass"):
            validate_production_config()


# ---------------------------------------------------------------------------
# (c) DEV_MODE=true alone resolves all five flags to True (back-compat)
# ---------------------------------------------------------------------------


class TestDevModeMetaFlag:
    def test_dev_mode_true_promotes_all_flags(self, monkeypatch) -> None:
        """DEV_MODE=true with no individual flags set → all five resolve True."""
        _clear_env(monkeypatch)
        monkeypatch.setenv("DEV_MODE", "true")

        settings = get_core_settings()
        assert settings.dev_auth_bypass is True
        assert settings.dev_error_detail is True
        assert settings.dev_cors_open is True
        assert settings.dev_smtp_log_only is True
        assert settings.dev_crypto_relaxed is True

    def test_dev_mode_false_leaves_flags_false(self, monkeypatch) -> None:
        """DEV_MODE=false (or unset) leaves all granular flags at their defaults."""
        _clear_env(monkeypatch)
        monkeypatch.setenv("DEV_MODE", "false")

        settings = get_core_settings()
        assert settings.dev_auth_bypass is False
        assert settings.dev_error_detail is False
        assert settings.dev_cors_open is False
        assert settings.dev_smtp_log_only is False
        assert settings.dev_crypto_relaxed is False

    def test_individual_flag_explicit_false_wins_over_dev_mode(self, monkeypatch) -> None:
        """An explicit DEV_AUTH_BYPASS=false overrides DEV_MODE=true."""
        _clear_env(monkeypatch)
        monkeypatch.setenv("DEV_MODE", "true")
        monkeypatch.setenv("DEV_AUTH_BYPASS", "false")

        settings = get_core_settings()
        # The explicitly-set flag must NOT be promoted
        assert settings.dev_auth_bypass is False
        # The others (not explicitly set) should still be promoted
        assert settings.dev_error_detail is True
        assert settings.dev_cors_open is True

    def test_individual_flag_explicit_true_without_dev_mode(self, monkeypatch) -> None:
        """DEV_CRYPTO_RELAXED=true with DEV_MODE unset works independently."""
        _clear_env(monkeypatch)
        monkeypatch.setenv("DEV_MODE", "false")
        monkeypatch.setenv("DEV_CRYPTO_RELAXED", "true")

        settings = get_core_settings()
        assert settings.dev_crypto_relaxed is True
        assert settings.dev_auth_bypass is False

    def test_lowercase_explicit_false_wins_over_dev_mode(self, monkeypatch) -> None:
        """A lowercase env var (dev_auth_bypass=false) must be honoured as an
        explicit opt-out even when DEV_MODE=true (case-insensitive check)."""
        _clear_env(monkeypatch)
        monkeypatch.setenv("DEV_MODE", "true")
        monkeypatch.setenv("dev_auth_bypass", "false")

        settings = get_core_settings()
        # Explicit lowercase opt-out must NOT be silently promoted back to True
        assert settings.dev_auth_bypass is False
        # Other flags (not explicitly set) are still promoted
        assert settings.dev_error_detail is True
        assert settings.dev_cors_open is True


# ---------------------------------------------------------------------------
# Production guard — each flag individually raises
# ---------------------------------------------------------------------------


class TestProductionGuardGranularFlags:
    _PROD_ENV = {"ENVIRONMENT": "production", "JARVIS_API_KEY": "a" * 32}

    def _setup_prod(self, monkeypatch) -> None:
        _clear_env(monkeypatch)
        for k, v in self._PROD_ENV.items():
            monkeypatch.setenv(k, v)

    @pytest.mark.parametrize(
        "flag_env",
        [
            "DEV_AUTH_BYPASS",
            "DEV_ERROR_DETAIL",
            "DEV_CORS_OPEN",
            "DEV_SMTP_LOG_ONLY",
            "DEV_CRYPTO_RELAXED",
        ],
    )
    def test_granular_flag_true_in_production_raises(self, monkeypatch, flag_env: str) -> None:
        """Each granular dev flag raises RuntimeError when set in production."""
        self._setup_prod(monkeypatch)
        monkeypatch.setenv(flag_env, "true")

        expected_field = flag_env.lower()
        with pytest.raises(RuntimeError, match=expected_field):
            validate_production_config()
