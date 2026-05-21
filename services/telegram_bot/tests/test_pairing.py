"""Tests for Sprint A /pair, /unpair, /whoami Telegram bot commands.

Covers:
- /pair <token>  happy path → upserts telegram_user_pairings, marks token consumed
- /pair <token>  expired token → replies error, deletes expired token
- /pair <token>  already consumed → replies error
- /pair          no args → replies usage
- /pair <token>  unknown token → replies error
- /unpair        paired chat → removes pairing, purges unconsumed tokens
- /unpair        not paired → replies informational
- /whoami        paired chat → shows paired-since timestamp (no raw user_id)
- /whoami        not paired → replies unpaired instructions
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest
from jarvis_common.testing import FakeAcquireCM, FakeTxnCM, make_bot_config, make_telegram_update
from telegram_bot.handlers.commands.pairing_commands import (
    pair_command,
    unpair_command,
    whoami_command,
)

# ---------------------------------------------------------------------------
# Test infrastructure helpers
# ---------------------------------------------------------------------------


def _make_config(telegram_chat_id: int | None = 777):
    return make_bot_config(telegram_chat_id=telegram_chat_id)


def _make_conn(
    fetchrow_return=None,
    fetchval_return=None,
    execute_return="EXECUTE 1",
):
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=fetchrow_return)
    conn.fetchval = AsyncMock(return_value=fetchval_return)
    conn.execute = AsyncMock(return_value=execute_return)
    conn.transaction = MagicMock(return_value=FakeTxnCM())
    return conn


def _make_pool(conn, *, fetchrow_return=None, fetchval_return=None, fetch_return=None):
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=FakeAcquireCM(conn))
    pool.fetchrow = AsyncMock(return_value=fetchrow_return)
    pool.fetchval = AsyncMock(return_value=fetchval_return)
    pool.fetch = AsyncMock(return_value=fetch_return or [])
    return pool


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
    update = make_telegram_update()
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
    update = make_telegram_update()
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
    update = make_telegram_update()
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
    update = make_telegram_update()
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
    conn = _make_conn()
    # First fetchrow: token lookup; second: UPSERT RETURNING (no rebound)
    conn.fetchrow = AsyncMock(
        side_effect=[
            {"user_id": 99, "expires_at": future, "consumed_at": None},
            {"was_update": False, "prior_chat_id": None},
        ]
    )
    pool = _make_pool(conn)
    update = make_telegram_update(chat_id=12345, username="alice")
    context = _make_context(pool, args=["validtoken123"])

    await pair_command(update, context)

    # One execute call: mark token consumed; upsert now uses fetchrow (RETURNING)
    assert conn.execute.await_count == 1
    consumed_sql = conn.execute.await_args_list[0].args[0]
    assert "consumed_at" in consumed_sql

    # UPSERT was issued via fetchrow (second call)
    assert conn.fetchrow.await_count == 2
    upsert_sql = conn.fetchrow.await_args_list[1].args[0]
    assert "telegram_user_pairings" in upsert_sql
    assert "ON CONFLICT" in upsert_sql

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
    update = make_telegram_update(chat_id=12345)
    config = _make_config(telegram_chat_id=12345)
    context = _make_context(pool, config=config, args=[])
    context.user_data = {"jarvis_user_id": 12345}

    # Multi-user mode requires a paired user_id; patch auth_check to return one.
    with patch(
        "telegram_bot.handlers.commands._auth.auth_check",
        new_callable=AsyncMock,
        return_value=(True, 12345),
    ):
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
    update = make_telegram_update(chat_id=777)
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
async def test_whoami_paired_chat_shows_paired_since():
    """Paired chat shows paired-since timestamp; must NOT leak the raw DB user_id."""
    paired_at = datetime(2025, 6, 1, 10, 30, tzinfo=UTC)
    row = {
        "user_id": 99,
        "telegram_username": "alice",
        "paired_at": paired_at,
    }

    pool = _make_pool(_make_conn(), fetchrow_return=row)
    update = make_telegram_update(chat_id=12345)
    context = _make_context(pool, args=[])

    await whoami_command(update, context)

    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert "Paired" in text
    assert "2025-06-01" in text  # paired-since date present
    # 3.14 (DOM-D-07): raw DB PK must not be in the reply
    assert not re.search(r"user_id=\d+", text), f"Raw user_id leaked in /whoami reply: {text!r}"
    assert "99" not in text, f"Raw numeric DB PK 99 leaked in /whoami reply: {text!r}"


@pytest.mark.asyncio
async def test_whoami_unpaired_chat_shows_instructions():
    """Unpaired chat shows how to pair."""
    pool = _make_pool(_make_conn(), fetchrow_return=None)
    update = make_telegram_update(chat_id=99999)
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
    update = make_telegram_update(chat_id=777)
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


# ---------------------------------------------------------------------------
# DOM-D-05: /pair rebound — second pairing from a new chat emits audit + notify
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pair_rebound_emits_system_event_and_notifies_prior_chat():
    """DOM-D-05: pairing from a new chat displacing an existing pairing must:
    - emit a system_events 'pairing.rebound' audit entry via log_event
    - attempt to notify the prior chat_id via bot.send_message
    - still succeed and reply Paired to the new chat
    """
    future = datetime.now(UTC) + timedelta(minutes=10)
    conn = _make_conn()
    # First fetchrow: token lookup; second: UPSERT RETURNING (rebound)
    upsert_result = {"was_update": True, "prior_chat_id": 1001}
    conn.fetchrow = AsyncMock(
        side_effect=[
            {"user_id": 55, "expires_at": future, "consumed_at": None},
            upsert_result,
        ]
    )
    pool = _make_pool(conn)
    # New pairing comes from chat 2002 (displaces 1001)
    update = make_telegram_update(chat_id=2002, username="bob")
    context = _make_context(pool, args=["newtoken"])
    context.bot = MagicMock()
    context.bot.send_message = AsyncMock()

    mock_log_event = AsyncMock()
    with patch(
        "telegram_bot.handlers.commands.pairing_commands.log_event",
        mock_log_event,
    ):
        await pair_command(update, context)

    # 1. log_event called with pairing.rebound
    mock_log_event.assert_awaited_once()
    log_kwargs = mock_log_event.await_args.kwargs
    assert log_kwargs["message"] == "pairing.rebound"
    assert log_kwargs["category"] == "auth"
    assert log_kwargs["context"]["prior_chat_id"] == 1001
    assert log_kwargs["context"]["new_chat_id"] == 2002

    # 2. Prior chat notified
    context.bot.send_message.assert_awaited_once()
    notify_args = context.bot.send_message.await_args
    called_chat = notify_args.args[0] if notify_args.args else notify_args.kwargs.get("chat_id")
    assert called_chat == 1001
    notified_text: str = notify_args.kwargs.get("text", "")
    assert "paired" in notified_text.lower() or "security" in notified_text.lower()

    # 3. New chat still gets success reply
    update.message.reply_text.assert_awaited_once()
    reply = update.message.reply_text.call_args[0][0]
    assert "Paired" in reply


@pytest.mark.asyncio
async def test_pair_rebound_stale_prior_chat_does_not_fail_new_pairing():
    """DOM-D-05: if notifying prior chat raises (stale/blocked), pairing still succeeds."""
    future = datetime.now(UTC) + timedelta(minutes=10)
    conn = _make_conn()
    upsert_result = {"was_update": True, "prior_chat_id": 9999}
    conn.fetchrow = AsyncMock(
        side_effect=[
            {"user_id": 77, "expires_at": future, "consumed_at": None},
            upsert_result,
        ]
    )
    pool = _make_pool(conn)
    update = make_telegram_update(chat_id=3003, username="carol")
    context = _make_context(pool, args=["token77"])
    context.bot = MagicMock()
    # Simulate blocked/stale prior chat raising
    context.bot.send_message = AsyncMock(side_effect=Exception("chat not found"))

    mock_log_event = AsyncMock()
    with patch(
        "telegram_bot.handlers.commands.pairing_commands.log_event",
        mock_log_event,
    ):
        await pair_command(update, context)

    # Pairing still succeeds despite failed notification
    update.message.reply_text.assert_awaited_once()
    reply = update.message.reply_text.call_args[0][0]
    assert "Paired" in reply


# ---------------------------------------------------------------------------
# Finding 3.12/3.14: UPSERT SQL must use CTE to capture pre-update chat_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pair_upsert_sql_uses_cte_for_prior_chat_id():
    """The UPSERT SQL must contain WITH prev AS … so prior_chat_id is the
    PRE-update value, not the post-update value that always equals the new
    chat_id (making the rebound branch permanently dead).
    """
    future = datetime.now(UTC) + timedelta(minutes=10)
    conn = _make_conn()
    conn.fetchrow = AsyncMock(
        side_effect=[
            {"user_id": 10, "expires_at": future, "consumed_at": None},
            {"was_update": False, "prior_chat_id": None},
        ]
    )
    pool = _make_pool(conn)
    update = make_telegram_update(chat_id=9999, username="user1")
    context = _make_context(pool, args=["ctetoken"])

    await pair_command(update, context)

    # Verify the UPSERT call (second fetchrow) uses the CTE pattern.
    assert conn.fetchrow.await_count == 2
    upsert_sql: str = conn.fetchrow.await_args_list[1].args[0]
    assert "WITH prev AS" in upsert_sql, (
        f"UPSERT SQL must use 'WITH prev AS' CTE to capture pre-update chat_id; got:\n{upsert_sql}"
    )
    assert "(SELECT chat_id FROM prev)" in upsert_sql, (
        "UPSERT RETURNING must reference (SELECT chat_id FROM prev)"
    )


# ---------------------------------------------------------------------------
# Finding N1: UNIQUE(chat_id) violation — chat already paired to another user
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pair_chat_already_paired_to_another_account():
    """If chat_id is already paired to user A and user B tries to pair the same
    chat, asyncpg raises UniqueViolationError.  The handler must catch it and
    reply with a clear 'already paired' message rather than a generic error.
    """
    future = datetime.now(UTC) + timedelta(minutes=10)
    conn = _make_conn()
    conn.fetchrow = AsyncMock(
        side_effect=[
            {"user_id": 20, "expires_at": future, "consumed_at": None},
            asyncpg.UniqueViolationError("duplicate key value violates unique constraint"),
        ]
    )
    pool = _make_pool(conn)
    update = make_telegram_update(chat_id=5555, username="userB")
    context = _make_context(pool, args=["tokenB"])

    await pair_command(update, context)

    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert "already paired" in text.lower() or "another account" in text.lower(), (
        f"Expected 'already paired' message for UniqueViolationError, got: {text!r}"
    )


# ---------------------------------------------------------------------------
# DOM-D-07: /whoami must not leak raw DB user_id (PK)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_whoami_does_not_leak_db_user_id():
    """DOM-D-07: /whoami reply must contain no 'user_id=<digits>' pattern."""
    paired_at = datetime.now(UTC)
    row = {
        "user_id": 12345678,
        "telegram_username": None,
        "paired_at": paired_at,
    }
    pool = _make_pool(_make_conn(), fetchrow_return=row)
    update = make_telegram_update(chat_id=99)
    context = _make_context(pool, args=[])

    await whoami_command(update, context)

    text = update.message.reply_text.call_args[0][0]
    assert not re.search(r"user_id=\d+", text), (
        f"DOM-D-07: raw DB PK leaked in /whoami reply: {text!r}"
    )
    assert "12345678" not in text, (
        f"DOM-D-07: raw numeric PK 12345678 leaked in /whoami reply: {text!r}"
    )


# ---------------------------------------------------------------------------
# DOS-3: /whoami rate-limit (5/minute per chat)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_whoami_rate_limited_after_five_calls() -> None:
    """DOS-3: whoami_command is decorated with @rate_limit(max_calls=5, window_seconds=60).

    After 5 allowed calls the 6th must be silently dropped (return None)
    without hitting the DB.
    """
    from telegram_bot.handlers.rate_limit import _timestamps

    _timestamps.clear()

    row = {
        "user_id": 1,
        "telegram_username": "tester",
        "paired_at": datetime(2025, 1, 1, tzinfo=UTC),
    }
    conn = _make_conn(fetchrow_return=row)
    pool = _make_pool(conn)
    chat_id = 888_001
    results: list = []
    for _ in range(6):
        update = make_telegram_update(chat_id=chat_id)
        context = _make_context(pool)
        result = await whoami_command(update, context)
        results.append(result)

    assert results[5] is None, (
        f"Expected 6th whoami call to be rate-limited (None) but got {results[5]!r}"
    )
    # whoami_command calls pool.fetchrow() directly (no acquire); 6th call must
    # not touch the DB — so fetchrow count must be exactly 5.
    assert pool.fetchrow.call_count == 5, (
        f"Expected 5 DB fetchrow calls (rate-limiter must stop 6th), got {pool.fetchrow.call_count}"
    )
