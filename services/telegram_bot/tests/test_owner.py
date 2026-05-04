"""Unit tests for app.owner.resolve_owner_chat_id."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import SecretStr
from telegram_bot.config import BotConfig
from telegram_bot.owner import resolve_owner_chat_id


def _make_config(chat_id: int | None) -> BotConfig:
    return BotConfig(
        telegram_token="tok",
        telegram_chat_id=chat_id,
        database_url="postgres://localhost/test",
        paper_ingestion_url="http://paper_ingestion:8000",
        learning_engine_url="http://learning_engine:8001",
        jarvis_api_key=SecretStr("key"),
    )


def _make_db_pool(fetchval_return) -> MagicMock:
    """Return a mock asyncpg Pool whose fetchval() resolves to *fetchval_return*."""
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=fetchval_return)
    return pool


@pytest.mark.asyncio
async def test_resolve_env_only():
    """When config.telegram_chat_id is set, it is returned without hitting DB."""
    config = _make_config(123)
    db_pool = _make_db_pool(None)

    result = await resolve_owner_chat_id(db_pool, config)

    assert result == 123
    db_pool.fetchval.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_db_only():
    """When config.telegram_chat_id is None, the DB row is queried and parsed."""
    config = _make_config(None)
    db_pool = _make_db_pool("456")

    result = await resolve_owner_chat_id(db_pool, config)

    assert result == 456
    db_pool.fetchval.assert_called_once()


@pytest.mark.asyncio
async def test_resolve_both_env_wins():
    """When both env and DB have values, the env value takes priority."""
    config = _make_config(100)
    # DB would return a different ID — it should never be consulted
    db_pool = _make_db_pool("999")

    result = await resolve_owner_chat_id(db_pool, config)

    assert result == 100
    db_pool.fetchval.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_neither_returns_none():
    """When both env and DB have no value, None is returned."""
    config = _make_config(None)
    db_pool = _make_db_pool(None)

    result = await resolve_owner_chat_id(db_pool, config)

    assert result is None


@pytest.mark.asyncio
async def test_resolve_db_null_string_returns_none():
    """DB value of literal 'null' (JSONB null serialised) must return None."""
    config = _make_config(None)
    db_pool = _make_db_pool("null")

    result = await resolve_owner_chat_id(db_pool, config)

    assert result is None


@pytest.mark.asyncio
async def test_resolve_db_invalid_value_returns_none():
    """Non-integer DB values degrade gracefully to None."""
    config = _make_config(None)
    db_pool = _make_db_pool("not-an-int")

    result = await resolve_owner_chat_id(db_pool, config)

    assert result is None


@pytest.mark.asyncio
async def test_resolve_db_exception_returns_none():
    """DB errors are swallowed and None is returned."""
    config = _make_config(None)
    db_pool = MagicMock()
    db_pool.fetchval = AsyncMock(side_effect=RuntimeError("connection lost"))

    result = await resolve_owner_chat_id(db_pool, config)

    assert result is None
