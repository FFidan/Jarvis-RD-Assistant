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

    assert config.telegram_token.get_secret_value() == "test-token"
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


def test_config_jarvis_base_url_defaults_to_none():
    """TG-BUG-01: jarvis_base_url defaults to None when JARVIS_BASE_URL is unset."""
    env = _minimal_env(chat_id="12345")
    with patch.dict(os.environ, env, clear=True):
        config = BotConfig.from_env()

    assert config.jarvis_base_url is None


def test_config_reads_jarvis_base_url_from_env():
    """TG-BUG-01: jarvis_base_url is sourced from JARVIS_BASE_URL."""
    env = _minimal_env(chat_id="12345")
    env["JARVIS_BASE_URL"] = "https://jarvis.example.com"
    with patch.dict(os.environ, env, clear=True):
        config = BotConfig.from_env()

    assert config.jarvis_base_url == "https://jarvis.example.com"


def test_config_reads_token_from_secret_file_when_env_unset(tmp_path):
    """Docker-secret convention: when only TELEGRAM_BOT_TOKEN_FILE is set (the bare
    TELEGRAM_BOT_TOKEN env is absent), the token is read from that secret file.

    Guards the bug where BotConfig (JarvisCommonSettings) does not apply the
    ``_FILE`` indirection, so the documented Docker-secret token path silently
    yielded an empty token and the bot exited.
    """
    secret = tmp_path / "telegram_bot_token"
    secret.write_text("123456:secret-token-from-file\n")
    env = {
        "TELEGRAM_BOT_TOKEN_FILE": str(secret),
        "DATABASE_URL": "postgres://localhost/test",
    }
    with patch.dict(os.environ, env, clear=True):
        config = BotConfig.from_env()

    assert config.telegram_token.get_secret_value() == "123456:secret-token-from-file"


def test_config_token_secret_file_oserror_falls_through_to_systemexit(tmp_path):
    """A TELEGRAM_BOT_TOKEN_FILE that cannot be read must fail safe, not raise OSError.

    read_text() on a directory raises IsADirectoryError (OSError subclass). The
    shared secret-file reader swallows it and yields None, so with no bare token
    and no DB row the token resolves empty and from_env exits cleanly.
    """
    env = {
        "TELEGRAM_BOT_TOKEN_FILE": str(tmp_path),  # a directory → IsADirectoryError
        "DATABASE_URL": "postgres://localhost/test",
    }
    with patch.dict(os.environ, env, clear=True):
        with pytest.raises(SystemExit):
            BotConfig.from_env()


def test_config_reads_jarvis_api_key_from_secret_file(tmp_path):
    """Docker-secret convention: when only JARVIS_API_KEY_FILE is set (the bare
    JARVIS_API_KEY env is absent), the key is read from that secret file.

    Guards the bug where an empty JARVIS_API_KEY caused every backend call to
    return 403.
    """
    secret = tmp_path / "jarvis_api_key"
    secret.write_text("my-secret-api-key\n")
    env = {
        "TELEGRAM_BOT_TOKEN": "test-token",
        "DATABASE_URL": "postgres://localhost/test",
        "JARVIS_API_KEY_FILE": str(secret),
    }
    with patch.dict(os.environ, env, clear=True):
        config = BotConfig.from_env()

    assert config.jarvis_api_key is not None
    assert config.jarvis_api_key.get_secret_value() == "my-secret-api-key"


def test_config_jarvis_base_url_javascript_scheme_rejected():
    """TG-03: a javascript: JARVIS_BASE_URL must be rejected at config-parse time.

    Prevents XSS / open-redirect via crafted deep-links in Telegram digests.
    ValidationError (pydantic) wraps the ValueError raised by _validate_base_url.
    """
    from pydantic import ValidationError

    env = _minimal_env(chat_id="12345")
    env["JARVIS_BASE_URL"] = "javascript:alert(1)"
    with patch.dict(os.environ, env, clear=True):
        with pytest.raises(ValidationError):
            BotConfig.from_env()


def test_config_jarvis_base_url_https_accepted():
    """TG-03: a well-formed https:// JARVIS_BASE_URL passes validation unchanged."""
    env = _minimal_env(chat_id="12345")
    env["JARVIS_BASE_URL"] = "https://jarvis.example.com"
    with patch.dict(os.environ, env, clear=True):
        config = BotConfig.from_env()

    assert config.jarvis_base_url == "https://jarvis.example.com"


# ---------------------------------------------------------------------------
# M12d — DSN credential redaction in the pool-creation log line
# ---------------------------------------------------------------------------


def test_redact_dsn_strips_userinfo_and_query():
    """_redact_dsn keeps only hostname:port/path — no userinfo, no query string."""
    from telegram_bot.config import _redact_dsn

    assert _redact_dsn("postgresql://u:pw@h:5432/db") == "h:5432/db"
    # Query strings can carry password= — they must be dropped too.
    assert _redact_dsn("postgresql://h/db?password=qpw") == "h/db"


@pytest.mark.asyncio
async def test_create_db_pool_log_redacts_credentials(caplog):
    """The 'Database pool created' log line must never leak DSN credentials.

    A DSN of the form user:password@host must surface as host:port/db only —
    neither the username nor the password substring may appear in the log.
    """
    import logging
    from unittest.mock import AsyncMock

    from telegram_bot.config import create_db_pool

    dsn = "postgresql://dbuser:hunter2@dbhost:5433/jarvis"
    with patch("telegram_bot.config.asyncpg.create_pool", new=AsyncMock(return_value=object())):
        with caplog.at_level(logging.INFO, logger="telegram_bot.config"):
            await create_db_pool(dsn)

    assert "Database pool created" in caplog.text
    assert "hunter2" not in caplog.text
    assert "dbuser" not in caplog.text
    assert "dbhost:5433/jarvis" in caplog.text


def test_config_jarvis_api_key_file_oserror_falls_through_to_none(tmp_path):
    """JARVIS_API_KEY_FILE pointing to an unreadable path must NOT raise.

    read_text() on a directory raises IsADirectoryError (OSError subclass).
    The OSError catch in BotConfig.from_env() must swallow it and leave
    jarvis_api_key as None (unauthenticated warning path).

    Verified: telegram_bot/config.py:183-190 — OSError branch sets api_key=None.
    """
    # A directory raises IsADirectoryError (OSError) on read_text().
    unreadable = tmp_path  # tmp_path itself is a directory
    env = {
        "TELEGRAM_BOT_TOKEN": "test-token",
        "DATABASE_URL": "postgres://localhost/test",
        "JARVIS_API_KEY_FILE": str(unreadable),
    }
    # Bare JARVIS_API_KEY is absent — only the FILE path is set.
    with patch.dict(os.environ, env, clear=True):
        config = BotConfig.from_env()

    # Must not raise; key falls through to None.
    assert config.jarvis_api_key is None
