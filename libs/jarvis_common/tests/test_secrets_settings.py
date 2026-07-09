"""Verify SecretsSettings honours both env-direct and _FILE indirection."""

from __future__ import annotations

from pathlib import Path

import pytest
from jarvis_common.settings import SecretsSettings, get_secrets_settings


def _isolated_env(monkeypatch, **kwargs):
    for name in (
        "JARVIS_API_KEY",
        "JARVIS_API_KEY_FILE",
        "JARVIS_MODEL_HMAC_KEY",
        "JARVIS_MODEL_HMAC_KEY_FILE",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_BOT_TOKEN_FILE",
        "LITELLM_MASTER_KEY",
        "LITELLM_MASTER_KEY_FILE",
        "JARVIS_CONFIG_KEY",
        "JARVIS_CONFIG_KEY_FILE",
        "JARVIS_CONFIG_KEY_OLD",
        "JARVIS_CONFIG_KEY_OLD_FILE",
        "SMTP_HOST",
        "SMTP_HOST_FILE",
        "SMTP_PORT",
        "SMTP_PORT_FILE",
        "SMTP_USER",
        "SMTP_USER_FILE",
        "SMTP_PASS",
        "SMTP_PASS_FILE",
        "SMTP_FROM",
        "SMTP_FROM_FILE",
        "SMTP_REPLY_TO",
        "SMTP_REPLY_TO_FILE",
        "SMTP_FROM_NAME",
        "SMTP_FROM_NAME_FILE",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in kwargs.items():
        monkeypatch.setenv(name, value)
    get_secrets_settings.cache_clear()


def test_env_direct(monkeypatch):
    _isolated_env(monkeypatch, JARVIS_API_KEY="env-direct-value")
    assert SecretsSettings().jarvis_api_key.get_secret_value() == "env-direct-value"


def test_file_indirection_overrides_env_direct(monkeypatch, tmp_path: Path):
    secret_file = tmp_path / "api.key"
    secret_file.write_text("file-value\n")
    _isolated_env(
        monkeypatch,
        JARVIS_API_KEY="env-direct-loses",
        JARVIS_API_KEY_FILE=str(secret_file),
    )
    assert SecretsSettings().jarvis_api_key.get_secret_value() == "file-value"


def test_missing_secret_resolves_to_empty(monkeypatch):
    _isolated_env(monkeypatch)
    assert SecretsSettings().jarvis_api_key is None


def test_get_secrets_settings_is_cached(monkeypatch):
    _isolated_env(monkeypatch, JARVIS_API_KEY="cached-value")
    a = get_secrets_settings()
    b = get_secrets_settings()
    assert a is b


def test_model_hmac_key_file_indirection(monkeypatch, tmp_path: Path):
    """HMAC-1: JARVIS_MODEL_HMAC_KEY_FILE resolves via the shared
    _FILE-indirection machinery (no bespoke code).
    """
    hex_key = "a" * 64
    secret_file = tmp_path / "model_hmac.key"
    secret_file.write_text(hex_key + "\n")
    _isolated_env(monkeypatch, JARVIS_MODEL_HMAC_KEY_FILE=str(secret_file))
    assert get_secrets_settings().jarvis_model_hmac_key.get_secret_value() == hex_key


def test_file_read_failure_raises_at_construction(monkeypatch):
    _isolated_env(
        monkeypatch,
        JARVIS_API_KEY_FILE="/nonexistent/path/does/not/exist",
    )
    with pytest.raises((RuntimeError, FileNotFoundError, OSError)):
        SecretsSettings()


def test_file_read_failure_does_not_fall_back_to_env(monkeypatch):
    _isolated_env(
        monkeypatch,
        JARVIS_API_KEY_FILE="/nonexistent/path/does/not/exist",
        JARVIS_API_KEY="fallback-env-value",
    )
    with pytest.raises((RuntimeError, FileNotFoundError, OSError)):
        SecretsSettings()


# ---------------------------------------------------------------------------
# smtp.* empty-string handling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "env_name",
    [
        "SMTP_HOST",
        "SMTP_PORT",
        "SMTP_FROM",
        "SMTP_USER",
        "SMTP_PASS",
        "SMTP_REPLY_TO",
        "SMTP_FROM_NAME",
    ],
)
def test_smtp_field_empty_string_treated_as_unset(monkeypatch, env_name: str) -> None:
    """Explicit empty SMTP env values are normalized to unset (``None``).

    ``.env.example`` ships these keys empty; raising would crash the process-wide
    ``SecretsSettings`` singleton, so an empty value means disabled, not an error.
    """
    _isolated_env(monkeypatch, **{env_name: ""})

    settings = SecretsSettings()

    assert getattr(settings, env_name.lower()) is None


def test_unset_smtp_fields_remain_allowed(monkeypatch) -> None:
    """Single-user/no-SMTP fallback remains available when SMTP vars are absent."""
    _isolated_env(monkeypatch)
    settings = SecretsSettings()

    assert settings.smtp_host is None
    assert settings.smtp_from is None
