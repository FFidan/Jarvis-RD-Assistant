"""Verify SecretsSettings honours both env-direct and _FILE indirection."""

from __future__ import annotations

from pathlib import Path

import pytest
from jarvis_common.settings import SecretsSettings, get_secrets_settings


def _isolated_env(monkeypatch, **kwargs):
    for name in (
        "JARVIS_API_KEY",
        "JARVIS_API_KEY_FILE",
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


def test_file_read_failure_raises_at_construction(monkeypatch):
    _isolated_env(
        monkeypatch,
        JARVIS_API_KEY_FILE="/nonexistent/path/does/not/exist",
    )
    with pytest.raises((RuntimeError, FileNotFoundError, OSError)):
        SecretsSettings()
