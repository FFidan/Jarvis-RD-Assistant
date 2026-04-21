"""Unit tests for BotConfig.from_env."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from telegram_bot.config import BotConfig


def _minimal_env(*, chat_id: str | None = "12345") -> dict[str, str]:
    """Return a minimal env dict with required vars set."""
    env: dict[str, str] = {
        "TELEGRAM_BOT_TOKEN": "test-token",
        "DATABASE_URL": "postgres://localhost/test",
    }
    if chat_id is not None:
        env["TELEGRAM_CHAT_ID"] = chat_id
    return env


def test_config_from_env_happy_path():
    """All required vars set → config loads with correct values."""
    env = _minimal_env(chat_id="99999")
    with patch.dict(os.environ, env, clear=True):
        config = BotConfig.from_env()

    assert config.telegram_token == "test-token"
    assert config.telegram_chat_id == 99999
    assert config.database_url == "postgres://localhost/test"


def test_config_from_env_without_chat_id_returns_none():
    """Missing TELEGRAM_CHAT_ID must NOT raise SystemExit; chat_id becomes None."""
    env = _minimal_env(chat_id=None)
    with patch.dict(os.environ, env, clear=True):
        config = BotConfig.from_env()

    assert config.telegram_chat_id is None


def test_config_from_env_invalid_chat_id_treated_as_none():
    """Non-integer TELEGRAM_CHAT_ID logs a warning and treats chat_id as None."""
    env = _minimal_env(chat_id="not-a-number")
    with patch.dict(os.environ, env, clear=True):
        config = BotConfig.from_env()

    assert config.telegram_chat_id is None


def test_config_from_env_missing_token_raises_systemexit():
    """Missing TELEGRAM_BOT_TOKEN must still raise SystemExit(1)."""
    env = {"DATABASE_URL": "postgres://localhost/test"}
    with patch.dict(os.environ, env, clear=True):
        with pytest.raises(SystemExit):
            BotConfig.from_env()


def test_config_from_env_missing_database_url_raises_systemexit():
    """Missing DATABASE_URL must still raise SystemExit(1)."""
    env = {"TELEGRAM_BOT_TOKEN": "tok"}
    with patch.dict(os.environ, env, clear=True):
        with pytest.raises(SystemExit):
            BotConfig.from_env()
