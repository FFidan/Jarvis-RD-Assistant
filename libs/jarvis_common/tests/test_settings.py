"""Tests for jarvis_common.settings env -> typed settings roundtrip."""

from __future__ import annotations

import pytest
from jarvis_common.settings import (
    CoreSettings,
    JobsSettings,
    RerankerSettings,
    SecretsSettings,
    TelegramSettings,
    get_core_settings,
    get_jobs_settings,
    get_reranker_settings,
    get_secrets_settings,
    get_telegram_settings,
)
from pydantic import ValidationError

# ---------------------------------------------------------------------------
# CoreSettings
# ---------------------------------------------------------------------------


def test_core_settings_does_not_expose_api_key():
    """CoreSettings must not have jarvis_api_key or jarvis_config_key; use get_secrets_settings()."""
    assert "jarvis_api_key" not in CoreSettings.model_fields, (
        "jarvis_api_key must live only in SecretsSettings"
    )
    assert "jarvis_config_key" not in CoreSettings.model_fields, (
        "jarvis_config_key must live only in SecretsSettings"
    )


def test_core_settings_defaults(monkeypatch):
    """With no env vars set, CoreSettings returns documented defaults."""
    for key in (
        "DEV_MODE",
        "LOG_LEVEL",
        "ENVIRONMENT",
        "TRUSTED_PROXY_HOSTS",
    ):
        monkeypatch.delenv(key, raising=False)

    settings = CoreSettings()
    assert settings.dev_mode is False
    assert settings.log_level == "INFO"
    assert settings.environment == "development"
    assert settings.trusted_proxy_hosts == "dashboard"
    assert settings.trusted_proxy_hosts_list == ["dashboard"]


def test_core_settings_reads_env(monkeypatch):
    """Env vars are picked up case-insensitively."""
    monkeypatch.setenv("DEV_MODE", "true")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("TRUSTED_PROXY_HOSTS", "dashboard,nginx,cf")

    settings = CoreSettings()
    assert settings.dev_mode is True
    assert settings.log_level == "DEBUG"
    assert settings.environment == "production"
    assert settings.trusted_proxy_hosts_list == ["dashboard", "nginx", "cf"]


def test_secrets_settings_do_not_leak_in_repr(monkeypatch):
    """SecretStr fields must mask their values in string representations."""
    monkeypatch.setenv("JARVIS_API_KEY", "leaky-key-value")
    monkeypatch.setenv("JARVIS_CONFIG_KEY", "leaky-fernet-value")
    secrets = SecretsSettings()
    # repr() and str() must not contain the raw secret
    assert "leaky-key-value" not in repr(secrets)
    assert "leaky-fernet-value" not in repr(secrets)
    assert "leaky-key-value" not in str(secrets)


def test_core_settings_trusted_proxy_list_ignores_empties(monkeypatch):
    """Whitespace-only entries are dropped from the parsed list."""
    monkeypatch.setenv("TRUSTED_PROXY_HOSTS", "  dashboard , , nginx ,")
    assert CoreSettings().trusted_proxy_hosts_list == ["dashboard", "nginx"]


def test_core_settings_dev_mode_invalid_raises(monkeypatch):
    """A non-boolean DEV_MODE raises a validation error."""
    monkeypatch.setenv("DEV_MODE", "not-a-bool")
    with pytest.raises(ValidationError):
        CoreSettings()


def test_secrets_settings_resolves_jarvis_api_key_file(tmp_path, monkeypatch):
    """SecretsSettings must honor JARVIS_API_KEY_FILE indirection."""
    key_file = tmp_path / "api_key"
    key_file.write_text("my-secret-test-value")
    monkeypatch.delenv("JARVIS_API_KEY", raising=False)
    monkeypatch.setenv("JARVIS_API_KEY_FILE", str(key_file))
    get_secrets_settings.cache_clear()
    s = SecretsSettings()
    assert s.jarvis_api_key is not None
    assert s.jarvis_api_key.get_secret_value() == "my-secret-test-value"


# ---------------------------------------------------------------------------
# RerankerSettings
# ---------------------------------------------------------------------------


def test_reranker_settings_default(monkeypatch):
    monkeypatch.delenv("RERANKER_ENABLED", raising=False)
    assert RerankerSettings().reranker_enabled is False


@pytest.mark.parametrize("raw, expected", [("true", True), ("1", True), ("false", False)])
def test_reranker_settings_accepts_truthy(monkeypatch, raw, expected):
    monkeypatch.setenv("RERANKER_ENABLED", raw)
    assert RerankerSettings().reranker_enabled is expected


def test_reranker_settings_invalid_raises(monkeypatch):
    monkeypatch.setenv("RERANKER_ENABLED", "bogus")
    with pytest.raises(ValidationError):
        RerankerSettings()


# ---------------------------------------------------------------------------
# JobsSettings
# ---------------------------------------------------------------------------


def test_jobs_settings_default(monkeypatch):
    monkeypatch.delenv("JARVIS_ENABLE_TEST_JOBS", raising=False)
    settings = JobsSettings()
    assert settings.jarvis_enable_test_jobs is None
    assert settings.test_jobs_enabled is False


def test_jobs_settings_enabled_only_when_exactly_one(monkeypatch):
    """Preserve the original `== "1"` semantics — anything else is disabled."""
    monkeypatch.setenv("JARVIS_ENABLE_TEST_JOBS", "1")
    assert JobsSettings().test_jobs_enabled is True

    monkeypatch.setenv("JARVIS_ENABLE_TEST_JOBS", "true")
    assert JobsSettings().test_jobs_enabled is False

    monkeypatch.setenv("JARVIS_ENABLE_TEST_JOBS", "")
    assert JobsSettings().test_jobs_enabled is False


# ---------------------------------------------------------------------------
# TelegramSettings
# ---------------------------------------------------------------------------


def test_telegram_settings_default(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_URL", raising=False)
    settings = TelegramSettings()
    assert settings.telegram_bot_url == ""
    assert settings.url_or_none is None


def test_telegram_settings_strips_and_preserves(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_URL", "  http://telegram_bot:8002  ")
    settings = TelegramSettings()
    assert settings.url_or_none == "http://telegram_bot:8002"


# ---------------------------------------------------------------------------
# Factory functions mirror fresh reads (no stale cache)
# ---------------------------------------------------------------------------


def test_factories_reflect_runtime_env_changes(monkeypatch):
    """Factories are intentionally uncached so monkeypatch.setenv is honoured."""
    monkeypatch.setenv("DEV_MODE", "false")
    assert get_core_settings().dev_mode is False

    monkeypatch.setenv("DEV_MODE", "true")
    assert get_core_settings().dev_mode is True

    monkeypatch.setenv("RERANKER_ENABLED", "true")
    assert get_reranker_settings().reranker_enabled is True

    monkeypatch.setenv("JARVIS_ENABLE_TEST_JOBS", "1")
    assert get_jobs_settings().test_jobs_enabled is True

    monkeypatch.setenv("TELEGRAM_BOT_URL", "http://host:9000")
    assert get_telegram_settings().url_or_none == "http://host:9000"


# ---------------------------------------------------------------------------
# DRY-C1 — _resolve_env_file_indirection shared by CoreSettings + SecretsSettings
# ---------------------------------------------------------------------------


def test_resolve_env_file_indirection_secrets_settings(tmp_path, monkeypatch):
    """SecretsSettings uses the _resolve_env_file_indirection hoisted helper.

    This test proves that a _FILE env var resolves correctly through
    SecretsSettings — confirming the factored-out function is wired in its
    model_validator.  CoreSettings no longer owns jarvis_api_key (CFG-DUP-1).
    """
    # Create a temp secret file
    secret_file = tmp_path / "api.key"
    secret_file.write_text("shared-secret-value\n")

    # Clear any pre-existing direct + file env vars that might interfere
    for key in ("JARVIS_API_KEY", "JARVIS_API_KEY_FILE"):
        monkeypatch.delenv(key, raising=False)

    monkeypatch.setenv("JARVIS_API_KEY_FILE", str(secret_file))

    get_secrets_settings.cache_clear()
    secrets = SecretsSettings()
    assert secrets.jarvis_api_key is not None
    assert secrets.jarvis_api_key.get_secret_value() == "shared-secret-value"

    # Raises on a missing file
    monkeypatch.setenv("JARVIS_API_KEY_FILE", "/nonexistent/secret.key")
    with pytest.raises((RuntimeError, OSError)):
        SecretsSettings()


def test_resolve_env_file_indirection_empty_file_resolves_to_none(tmp_path, monkeypatch):
    """An empty (whitespace-only) secret file resolves to None, not an empty string."""
    empty_file = tmp_path / "empty.key"
    empty_file.write_text("   \n  ")
    for key in ("JARVIS_API_KEY", "JARVIS_API_KEY_FILE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("JARVIS_API_KEY_FILE", str(empty_file))
    get_secrets_settings.cache_clear()
    settings = SecretsSettings()
    assert settings.jarvis_api_key is None
