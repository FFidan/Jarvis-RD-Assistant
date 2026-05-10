"""Tests for Sprint A /pair, /unpair, /whoami Telegram bot commands.

Covers:
- /pair <token>  happy path → upserts telegram_user_pairings, marks token consumed
- /pair <token>  expired token → replies error, deletes expired token
- /pair <token>  already consumed → replies error
- /pair          no args → replies usage
- /pair <token>  unknown token → replies error
- /unpair        paired chat → removes pairing, purges unconsumed tokens
- /unpair        not paired → replies informational
- /whoami        paired chat → replies user_id + chat_id
- /whoami        not paired → replies unpaired instructions
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import SecretStr
from telegram_bot.config import BotConfig
from telegram_bot.handlers.commands.pairing_commands import (
    pair_command,
    unpair_command,
    whoami_command,
)

# ---------------------------------------------------------------------------
# Test infrastructure helpers
# ---------------------------------------------------------------------------


def _make_config(telegram_chat_id: int | None = 777) -> BotConfig:
    return BotConfig(
        telegram_token="test-token",
        telegram_chat_id=telegram_chat_id,
        database_url="postgres://test",
        paper_ingestion_url="http://paper:8000",
        learning_engine_url="http://learn:8001",
        jarvis_api_key=SecretStr("test-key"),
    )


class _FakeTxnCM:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None


class _FakeAcquireCM:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *_):
        return None


def _make_conn(
    fetchrow_return=None,
    fetchval_return=None,
    execute_return="EXECUTE 1",
):
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=fetchrow_return)
    conn.fetchval = AsyncMock(return_value=fetchval_return)
    conn.execute = AsyncMock(return_value=execute_return)
    conn.transaction = MagicMock(return_value=_FakeTxnCM())
    return conn


def _make_pool(conn, *, fetchrow_return=None, fetchval_return=None, fetch_return=None):
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_FakeAcquireCM(conn))
    pool.fetchrow = AsyncMock(return_value=fetchrow_return)
    pool.fetchval = AsyncMock(return_value=fetchval_return)
    pool.fetch = AsyncMock(return_value=fetch_return or [])
    return pool


def _make_update(
    text: str = "",
    chat_id: int = 42,
    username: str | None = "testuser",
):
    update = MagicMock()
    update.effective_chat = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_chat.username = username
    update.message = MagicMock()
    update.message.text = text
    update.message.reply_text = AsyncMock()
    return update


def _make_context(pool, config=None, args=None):
    context = MagicMock()
    context.args = args or []
    context.application = MagicMock()
    context.application.bot_data = {
        "config": config or _make_config(),
        "db_pool": pool,
        "http_client": AsyncMock(),
    }
    return context


# ---------------------------------------------------------------------------
# /pair tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pair_no_args_replies_usage():
    """Calling /pair without a token replies with usage instructions."""
    conn = _make_conn()
    pool = _make_pool(conn)
    update = _make_update()
    context = _make_context(pool, args=[])

    await pair_command(update, context)

    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert "/pair" in text


@pytest.mark.asyncio
async def test_pair_unknown_token_replies_error():
    """Unknown token triggers an informative error reply."""
    conn = _make_conn(fetchrow_return=None)
    pool = _make_pool(conn)
    update = _make_update()
    context = _make_context(pool, args=["unknowntoken"])

    await pair_command(update, context)

    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert "Invalid" in text or "Unrecognised" in text.lower() or "unrecognised" in text.lower()


@pytest.mark.asyncio
async def test_pair_already_consumed_token_replies_error():
    """Token that has already been consumed is rejected with clear error."""
    conn = _make_conn(
        fetchrow_return={
            "user_id": 10,
            "expires_at": datetime.now(UTC) + timedelta(minutes=10),
            "consumed_at": datetime.now(UTC) - timedelta(minutes=5),
        }
    )
    pool = _make_pool(conn)
    update = _make_update()
    context = _make_context(pool, args=["alreadyused"])

    await pair_command(update, context)

    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert "already been used" in text


@pytest.mark.asyncio
async def test_pair_expired_token_deletes_and_replies_error():
    """Expired token is deleted and user gets a clear expiry error."""
    conn = _make_conn(
        fetchrow_return={
            "user_id": 10,
            "expires_at": datetime.now(UTC) - timedelta(minutes=5),
            "consumed_at": None,
        }
    )
    pool = _make_pool(conn)
    update = _make_update()
    context = _make_context(pool, args=["expiredtoken"])

    await pair_command(update, context)

    # Should have deleted the expired token
    assert conn.execute.await_count >= 1
    delete_sql = conn.execute.await_args_list[0].args[0]
    assert "DELETE FROM telegram_pairing_tokens" in delete_sql

    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert "expired" in text.lower()


@pytest.mark.asyncio
async def test_pair_valid_token_upserts_pairing_and_marks_consumed():
    """Valid token upserts telegram_user_pairings and marks token consumed."""
    future = datetime.now(UTC) + timedelta(minutes=10)
    conn = _make_conn(
        fetchrow_return={
            "user_id": 99,
            "expires_at": future,
            "consumed_at": None,
        }
    )
    pool = _make_pool(conn)
    update = _make_update(chat_id=12345, username="alice")
    context = _make_context(pool, args=["validtoken123"])

    await pair_command(update, context)

    # Two execute calls: upsert pairings + mark consumed
    assert conn.execute.await_count == 2
    upsert_sql = conn.execute.await_args_list[0].args[0]
    consumed_sql = conn.execute.await_args_list[1].args[0]
    assert "telegram_user_pairings" in upsert_sql
    assert "INSERT" in upsert_sql or "ON CONFLICT" in upsert_sql
    assert "consumed_at" in consumed_sql

    # Success reply
    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert "Paired" in text


# ---------------------------------------------------------------------------
# /unpair tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unpair_paired_chat_removes_pairing():
    """Unpair deletes the pairing row and confirms success."""
    conn = _make_conn(fetchval_return=42, execute_return="DELETE 1")
    pool = _make_pool(conn)
    update = _make_update(chat_id=12345)
    # Patch auth_required: the decorator calls auth_check → DB query.
    # We set telegram_chat_id to match chat_id so auth passes.
    config = _make_config(telegram_chat_id=12345)
    context = _make_context(pool, config=config, args=[])

    await unpair_command(update, context)

    # Should have called execute at least once with DELETE
    executed_sqls = [c.args[0] for c in conn.execute.await_args_list]
    assert any("DELETE FROM telegram_user_pairings" in s for s in executed_sqls)
    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert "Unpaired" in text


@pytest.mark.asyncio
async def test_unpair_not_paired_chat_replies_informational():
    """Unpair with no active pairing gives an informational reply."""
    conn = _make_conn(fetchval_return=None, execute_return="DELETE 0")
    pool = _make_pool(conn)
    update = _make_update(chat_id=777)
    config = _make_config(telegram_chat_id=777)
    context = _make_context(pool, config=config, args=[])

    await unpair_command(update, context)

    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    # Should say no pairing was found or similar
    assert "pairing" in text.lower()


# ---------------------------------------------------------------------------
# /whoami tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_whoami_paired_chat_shows_user_id():
    """Paired chat shows user_id and chat_id in the reply."""
    paired_at = datetime.now(UTC)
    # asyncpg Record-like object
    row = {
        "user_id": 99,
        "telegram_username": "alice",
        "paired_at": paired_at,
    }

    pool = _make_pool(_make_conn(), fetchrow_return=row)
    update = _make_update(chat_id=12345)
    context = _make_context(pool, args=[])

    await whoami_command(update, context)

    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert "Paired" in text
    assert "99" in text  # user_id
    assert "12345" in text  # chat_id


@pytest.mark.asyncio
async def test_whoami_unpaired_chat_shows_instructions():
    """Unpaired chat shows how to pair."""
    pool = _make_pool(_make_conn(), fetchrow_return=None)
    update = _make_update(chat_id=99999)
    config = _make_config(telegram_chat_id=777)  # different chat → not legacy owner
    context = _make_context(pool, config=config, args=[])

    await whoami_command(update, context)

    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert "not paired" in text.lower() or "pair" in text.lower()


@pytest.mark.asyncio
async def test_whoami_legacy_owner_shows_system_owner_message():
    """Chat matching config.telegram_chat_id but no per-user pairing gets legacy-owner message."""
    pool = _make_pool(_make_conn(), fetchrow_return=None)
    update = _make_update(chat_id=777)
    config = _make_config(telegram_chat_id=777)
    context = _make_context(pool, config=config, args=[])

    await whoami_command(update, context)

    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    # Should mention legacy / system owner
    assert (
        "system owner" in text.lower()
        or "legacy" in text.lower()
        or "single-tenant" in text.lower()
    )
