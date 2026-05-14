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

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import SecretStr
from telegram_bot.config import BotConfig
from telegram_bot.handlers.commands import start_command  # noqa: E402
from telegram_bot.handlers.commands.pairing_commands import pair_command  # noqa: E402
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
        jarvis_api_key=SecretStr("test-key"),
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

    # Two execute calls: INSERT … ON CONFLICT (upsert) into user_config, DELETE telegram_pairing
    assert conn.execute.await_count == 2
    upsert_call = conn.execute.await_args_list[0]
    assert "user_config" in upsert_call.args[0]
    # Fix H4: chat.id is passed directly (int), not json.dumps(int) — asyncpg
    # JSONB codec applies json.dumps internally, so the stored value is a JSON
    # number, not a JSON string.
    assert upsert_call.args[1] == 42
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


# ---------------------------------------------------------------------------
# Regression: pairing must INSERT (not UPDATE) so fresh installs work
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_telegram_pairing_inserts_on_fresh_install():
    """SEC-102: pairing uses INSERT … ON CONFLICT so it works on a fresh install.

    A bare UPDATE would silently update 0 rows if 'telegram.owner_chat_id'
    doesn't exist yet (e.g. init.sql seeded it as null but a manual wipe removed
    the row).  The correct fix is an upsert.
    """
    future = datetime.now(UTC) + timedelta(minutes=5)
    conn = _make_conn(fetchrow_return={"expires_at": future})
    pool = _make_pool(conn)
    update = _make_update("/start PAIR_FRESHDB", chat_id=99)
    # fresh install: no env-var TELEGRAM_CHAT_ID configured
    context = _make_context(pool, _make_config(telegram_chat_id=None))

    await start_command(update, context)

    # The user_config write must use INSERT … ON CONFLICT, not bare UPDATE.
    upsert_call = conn.execute.await_args_list[0]
    sql: str = upsert_call.args[0]
    assert "INSERT INTO user_config" in sql, (
        f"Expected INSERT INTO user_config upsert but got: {sql!r}"
    )
    assert "ON CONFLICT" in sql, f"Expected ON CONFLICT clause but got: {sql!r}"
    # Fix H4: chat.id is passed as a native int; asyncpg codec serialises it.
    assert upsert_call.args[1] == 99
    # Success reply must be sent
    update.message.reply_text.assert_awaited_once()
    reply_text = update.message.reply_text.call_args[0][0]
    assert "Paired" in reply_text


# ---------------------------------------------------------------------------
# Regression H4: chat.id must reach asyncpg as a native int (no double-encode)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pairing_stores_chat_id_as_native_int_not_json_string():
    """H4 regression: _handle_pairing passes chat.id directly, not json.dumps(chat.id).

    asyncpg's JSONB codec (encoder=json.dumps) serialises the value itself.
    Wrapping in json.dumps first produces a JSON *string* ("42") instead of a
    JSON *number* (42) — i.e. jsonb_typeof would return 'string', not 'number'.

    This test verifies via a codec round-trip that the value received by
    conn.execute is a native Python int, not a pre-serialised string.
    """
    import json as _json

    future = datetime.now(UTC) + timedelta(minutes=5)
    conn = _make_conn(fetchrow_return={"expires_at": future})
    pool = _make_pool(conn)
    update = _make_update("/start PAIR_H4TEST", chat_id=123456789)
    context = _make_context(pool, _make_config(telegram_chat_id=None))

    await start_command(update, context)

    upsert_call = conn.execute.await_args_list[0]
    raw_value = upsert_call.args[1]

    # The value passed must be a native int, not a pre-serialised string.
    assert isinstance(raw_value, int), (
        f"Expected int but got {type(raw_value).__name__!r}: {raw_value!r}. "
        "Pass chat.id directly — asyncpg's JSONB codec serialises it."
    )
    assert raw_value == 123456789

    # Codec round-trip: json.dumps(int) → '123456789' → json.loads → int
    # This is what Postgres stores; jsonb_typeof would be 'number', not 'string'.
    encoded = _json.dumps(raw_value)
    decoded = _json.loads(encoded)
    assert isinstance(decoded, int), (
        f"After JSONB codec round-trip, value should decode as int, got {type(decoded)}"
    )
    assert decoded == 123456789


# ---------------------------------------------------------------------------
# DOM-D-02: /pair rate limit (5/minute per chat)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pair_command_rate_limited_after_five_calls() -> None:
    """DOM-D-02: pair_command is decorated with @rate_limit(max_calls=5, window_seconds=60).

    Rapid-fire calls from the same chat_id must be silently dropped (return
    None) after the 5th call.  The DB pool is configured to always return a
    valid unexpired token so the handler would succeed without the rate limit.
    """
    from telegram_bot.handlers.rate_limit import _timestamps

    _timestamps.clear()

    future = datetime.now(UTC) + timedelta(minutes=5)
    conn = _make_conn(fetchrow_return={"user_id": 1, "expires_at": future, "consumed_at": None})
    pool = _make_pool(conn)
    config = _make_config(telegram_chat_id=None)

    chat_id = 555_001
    results: list = []
    for _ in range(6):
        update = _make_update("/pair abc123", chat_id=chat_id)
        context = _make_context(pool, config)
        context.args = ["abc123"]
        result = await pair_command(update, context)
        results.append(result)

    # First 5 calls must NOT be rate-limited (they reach the handler and either
    # succeed or fail for DB reasons, but do not return None due to rate limiting).
    # The 6th call must be rate-limited (return None without reaching the DB).
    assert results[5] is None, f"Expected 6th call to be rate-limited (None) but got {results[5]!r}"
    # The 6th call must NOT have triggered a DB transaction (rate-limiter fires before DB).
    # Each successful pairing attempt acquires the pool once.  After 5 calls, the
    # 6th is stopped before acquire(), so total acquire count must be exactly 5.
    assert pool.acquire.call_count == 5, (
        f"Expected 5 DB acquires (one per non-rate-limited call), got {pool.acquire.call_count}"
    )
