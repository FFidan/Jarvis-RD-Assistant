"""Unit contracts for the database-free Telegram configuration boundary."""

from __future__ import annotations

import os
from collections.abc import Iterator
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError
from telegram_bot.config import BotConfig


def _minimal_env() -> dict[str, str]:
    return {
        "TELEGRAM_BOT_TOKEN": "test-token",
        "JARVIS_TELEGRAM_SERVICE_TOKEN": "service-token-with-at-least-32-characters",
    }


@pytest.fixture(autouse=True)
def _platform_token_unavailable() -> Iterator[AsyncMock]:
    lookup = AsyncMock(return_value=None)
    with patch("telegram_bot.config._read_platform_bot_token", lookup):
        yield lookup


def test_config_from_env_requires_no_database_or_config_key() -> None:
    with patch.dict(os.environ, _minimal_env(), clear=True):
        config = BotConfig.from_env()

    assert config.telegram_token.get_secret_value() == "test-token"
    assert config.telegram_service_token.get_secret_value().startswith("service-token")


@pytest.mark.parametrize(
    "missing",
    ["TELEGRAM_BOT_TOKEN", "JARVIS_TELEGRAM_SERVICE_TOKEN"],
)
def test_config_missing_required_credential_exits(missing: str) -> None:
    env = _minimal_env()
    env.pop(missing)
    with patch.dict(os.environ, env, clear=True), pytest.raises(SystemExit):
        BotConfig.from_env()


def test_config_jarvis_base_url_defaults_to_none() -> None:
    with patch.dict(os.environ, _minimal_env(), clear=True):
        config = BotConfig.from_env()

    assert config.jarvis_base_url is None


def test_config_reads_jarvis_base_url_from_env() -> None:
    env = {**_minimal_env(), "JARVIS_BASE_URL": "https://jarvis.example.com/"}
    with patch.dict(os.environ, env, clear=True):
        config = BotConfig.from_env()

    assert config.jarvis_base_url == "https://jarvis.example.com"


def test_config_reads_both_credentials_from_secret_files(tmp_path) -> None:
    bot_secret = tmp_path / "telegram_bot_token"
    service_secret = tmp_path / "telegram_service_token"
    bot_secret.write_text("123456:secret-token-from-file\n")
    service_secret.write_text("dedicated-service-token-with-32-characters\n")
    env = {
        "TELEGRAM_BOT_TOKEN_FILE": str(bot_secret),
        "JARVIS_TELEGRAM_SERVICE_TOKEN_FILE": str(service_secret),
    }
    with patch.dict(os.environ, env, clear=True):
        config = BotConfig.from_env()

    assert config.telegram_token.get_secret_value() == "123456:secret-token-from-file"
    assert config.telegram_service_token.get_secret_value().startswith("dedicated-service")


def test_platform_bot_token_overrides_bootstrap_secret(
    _platform_token_unavailable: AsyncMock,
) -> None:
    _platform_token_unavailable.return_value = "wizard-saved-token"
    with patch.dict(os.environ, _minimal_env(), clear=True):
        config = BotConfig.from_env()

    assert config.telegram_token.get_secret_value() == "wizard-saved-token"


@pytest.mark.parametrize(
    "file_variable",
    ["TELEGRAM_BOT_TOKEN_FILE", "JARVIS_TELEGRAM_SERVICE_TOKEN_FILE"],
)
def test_unreadable_secret_file_fails_safe(tmp_path, file_variable: str) -> None:
    env = _minimal_env()
    direct_variable = (
        "TELEGRAM_BOT_TOKEN"
        if file_variable == "TELEGRAM_BOT_TOKEN_FILE"
        else "JARVIS_TELEGRAM_SERVICE_TOKEN"
    )
    env.pop(direct_variable)
    env[file_variable] = str(tmp_path)
    with patch.dict(os.environ, env, clear=True), pytest.raises(SystemExit):
        BotConfig.from_env()


def test_config_rejects_javascript_public_url() -> None:
    env = {**_minimal_env(), "JARVIS_BASE_URL": "javascript:alert(1)"}
    with patch.dict(os.environ, env, clear=True), pytest.raises(ValidationError):
        BotConfig.from_env()


def test_config_rejects_credentials_in_service_url() -> None:
    env = {**_minimal_env(), "PLATFORM_API_URL": "http://user:password@platform_api:8003"}
    with patch.dict(os.environ, env, clear=True), pytest.raises(ValidationError):
        BotConfig.from_env()


def test_bot_config_has_numpy_style_public_docstring() -> None:
    doc = BotConfig.__doc__ or ""
    assert "Parameters\n----------" in doc
    for field in (
        "telegram_token",
        "telegram_service_token",
        "platform_api_url",
        "paper_ingestion_url",
        "learning_engine_url",
        "jarvis_base_url",
    ):
        assert field in doc
