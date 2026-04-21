"""Tests for the /start PAIR_<code> deep-link pairing flow and DB-fallback auth.

Covers:
- /start PAIR_<valid>   -> persists chat id into user_config, replies success
- /start PAIR_<expired> -> deletes code, replies error
- /start PAIR_<unknown> -> replies error
- /start (no payload)   -> still auth-gated; unauthed chat yields no reply
- _auth_check: env-var priority over DB
- _auth_check: DB fallback when env-var is None
- _auth_check: DB null -> unauthorised
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram_bot.config import BotConfig
from telegram_bot.handlers.commands import start_command  # noqa: E402
from telegram_bot.handlers.helpers import auth_check as _auth_check  # noqa: E402

_OWNER_CHAT_ID = 777


def _make_config(telegram_chat_id: int | None = _OWNER_CHAT_ID) -> BotConfig:
    # BotConfig is frozen and types telegram_chat_id as int, but at runtime
    # dataclass(frozen=True) doesn't enforce type hints, so passing None works
    # for the auth-fallback path tests.
    return BotConfig(
        telegram_token="test-token",
        telegram_chat_id=telegram_chat_id,  # type: ignore[arg-type]
        database_url="postgres://test",
        paper_ingestion_url="http://paper:8000",
        learning_engine_url="http://learn:8001",
        jarvis_api_key="test-key",
    )


class _FakeAcquireCM:
    """Async context manager returned by ``pool.acquire()``."""

    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *_):
        return None


class _FakeTxnCM:
    """Async context manager returned by ``conn.transaction()``."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None


def _make_conn(fetchrow_return=None, fetchrow_side_effect=None, fetchval_return=None):
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=fetchrow_return, side_effect=fetchrow_side_effect)
    conn.fetchval = AsyncMock(return_value=fetchval_return)  # existing-owner check
    conn.execute = AsyncMock(return_value="EXECUTE 1")
    conn.transaction = MagicMock(return_value=_FakeTxnCM())
    return conn


def _make_pool(conn, *, fetchval_return=None):
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_FakeAcquireCM(conn))
    pool.fetchval = AsyncMock(return_value=fetchval_return)
    return pool


def _make_update(text: str, chat_id: int = _OWNER_CHAT_ID):
    update = MagicMock()
    update.effective_chat = MagicMock()
    update.effective_chat.id = chat_id
    update.message = MagicMock()
    update.message.text = text
    update.message.reply_text = AsyncMock()
    return update


def _make_context(pool, config):
    context = MagicMock()
    context.application = MagicMock()
    context.application.bot_data = {
        "config": config,
        "db_pool": pool,
        "http_client": AsyncMock(),
    }
    return context


# ---------------------------------------------------------------------------
# /start PAIR_* tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_with_valid_pair_code_stores_chat_id():
    """Valid pairing code persists chat.id into user_config and confirms."""
    future = datetime.now(UTC) + timedelta(minutes=5)
    conn = _make_conn(fetchrow_return={"expires_at": future})
    pool = _make_pool(conn)
    update = _make_update("/start PAIR_ABC123", chat_id=42)
    context = _make_context(pool, _make_config(telegram_chat_id=None))

    await start_command(update, context)

    # fetchrow took the code
    assert conn.fetchrow.await_count == 1
    assert conn.fetchrow.await_args.args[1] == "ABC123"

    # Two execute calls: UPDATE user_config, DELETE telegram_pairing
    assert conn.execute.await_count == 2
    upsert_call = conn.execute.await_args_list[0]
    assert "user_config" in upsert_call.args[0]
    assert upsert_call.args[1] == json.dumps(42)
    delete_call = conn.execute.await_args_list[1]
    assert "DELETE FROM telegram_pairing" in delete_call.args[0]
    assert delete_call.args[1] == "ABC123"

    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert "Paired" in text


@pytest.mark.asyncio
async def test_start_with_expired_pair_code_replies_error():
    """Expired pairing code is deleted and user gets an error reply."""
    past = datetime.now(UTC) - timedelta(minutes=5)
    conn = _make_conn(fetchrow_return={"expires_at": past})
    pool = _make_pool(conn)
    update = _make_update("/start PAIR_STALE", chat_id=42)
    context = _make_context(pool, _make_config(telegram_chat_id=None))

    await start_command(update, context)

    # Exactly one execute (DELETE of the stale code) and no user_config write
    assert conn.execute.await_count == 1
    del_call = conn.execute.await_args_list[0]
    assert "DELETE FROM telegram_pairing" in del_call.args[0]
    assert del_call.args[1] == "STALE"

    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert "Invalid or expired" in text


@pytest.mark.asyncio
async def test_start_with_unknown_pair_code_replies_error():
    """Unknown pairing code yields the same error, no DB writes."""
    conn = _make_conn(fetchrow_return=None)
    pool = _make_pool(conn)
    update = _make_update("/start PAIR_NOPE", chat_id=42)
    context = _make_context(pool, _make_config(telegram_chat_id=None))

    await start_command(update, context)

    assert conn.execute.await_count == 0
    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert "Invalid or expired" in text


@pytest.mark.asyncio
async def test_start_without_payload_requires_auth():
    """Plain /start from an unauthorised chat sends no reply."""
    pool = _make_pool(_make_conn(), fetchval_return=None)  # DB null
    update = _make_update("/start", chat_id=999)  # not owner, not in DB
    context = _make_context(pool, _make_config(telegram_chat_id=_OWNER_CHAT_ID))

    await start_command(update, context)

    update.message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_without_payload_authed_sends_welcome():
    """Plain /start from the authorised chat sends the welcome message."""
    pool = _make_pool(_make_conn(), fetchval_return=None)
    update = _make_update("/start", chat_id=_OWNER_CHAT_ID)
    context = _make_context(pool, _make_config(telegram_chat_id=_OWNER_CHAT_ID))

    await start_command(update, context)

    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert "Welcome" in text


# ---------------------------------------------------------------------------
# _auth_check fallback tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_check_env_var_priority():
    """Env var match returns True even when DB has no paired chat."""
    pool = _make_pool(_make_conn(), fetchval_return=None)
    update = _make_update("irrelevant", chat_id=_OWNER_CHAT_ID)
    config = _make_config(telegram_chat_id=_OWNER_CHAT_ID)

    assert await _auth_check(update, config, pool) is True
    # DB is NOT consulted when env var already matched
    pool.fetchval.assert_not_awaited()


@pytest.mark.asyncio
async def test_auth_check_db_fallback():
    """When env var is missing, DB-stored chat id grants access."""
    pool = _make_pool(_make_conn(), fetchval_return=555)
    update = _make_update("irrelevant", chat_id=555)
    config = _make_config(telegram_chat_id=None)

    assert await _auth_check(update, config, pool) is True
    pool.fetchval.assert_awaited_once()


@pytest.mark.asyncio
async def test_auth_check_db_null():
    """Env var missing + DB null -> unauthorised."""
    pool = _make_pool(_make_conn(), fetchval_return=None)
    update = _make_update("irrelevant", chat_id=555)
    config = _make_config(telegram_chat_id=None)

    assert await _auth_check(update, config, pool) is False


@pytest.mark.asyncio
async def test_auth_check_db_string_coerced():
    """DB value stored as a string is still coerced to int for comparison."""
    pool = _make_pool(_make_conn(), fetchval_return="555")
    update = _make_update("irrelevant", chat_id=555)
    config = _make_config(telegram_chat_id=None)

    assert await _auth_check(update, config, pool) is True
