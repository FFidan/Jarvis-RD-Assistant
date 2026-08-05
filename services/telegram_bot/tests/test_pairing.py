"""Tests for /pair, /unpair, /whoami Telegram bot commands.

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
from functools import partial
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest
from jarvis_common.testing import (
    FakeTxnCM,
    PTBContextOptions,
    make_bot_config,
    make_conn,
    make_pool_and_conn,
    make_ptb_context,
    make_telegram_update,
)
from telegram_bot.config import BotConfig
from telegram_bot.handlers.commands.pairing_commands import (
    pair_command,
    unpair_command,
    whoami_command,
)

# ---------------------------------------------------------------------------
# Test infrastructure helpers
# ---------------------------------------------------------------------------


def _make_config(telegram_chat_id: int | None = 777):
    return make_bot_config(BotConfig, telegram_chat_id=telegram_chat_id)


_make_conn = partial(
    make_conn,
    fetchrow_return=None,
    fetchval_return=None,
    execute_return="EXECUTE 1",
)


def _make_pool(conn, *, fetchrow_return=None, fetchval_return=None, fetch_return=None):
    # conn is used as-is (with_transaction=False): tests wire their own txn CMs.
    # Pool-level lookups intentionally return different rows than the conn-level
    # command flow, so they stay independent mocks layered on the shared factory.
    pool, _conn = make_pool_and_conn(conn=conn, with_transaction=False)
    pool.fetchrow = AsyncMock(return_value=fetchrow_return)
    pool.fetchval = AsyncMock(return_value=fetchval_return)
    pool.fetch = AsyncMock(return_value=fetch_return or [])
    return pool


def _make_context(
    pool: object,
    config: BotConfig | None = None,
    args: list[str] | None = None,
) -> MagicMock:
    return make_ptb_context(
        pool,
        config or _make_config(),
        options=PTBContextOptions(args=args),
    )


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
async def test_pair_in_group_chat_rejected_without_touching_db():
    """/pair sent from a group chat is rejected BEFORE the token is consumed.

    Identity binds to chat_id, so pairing in a group would grant every member
    the paired identity.  Even with a valid token available in
    the DB, a non-private chat must early-return with the "1:1 chat only"
    message and never acquire the pool / upsert a row.
    """
    conn = _make_conn(
        fetchrow_return={
            "user_id": 5,
            "expires_at": datetime.now(UTC) + timedelta(minutes=10),
            "consumed_at": None,
        }
    )
    pool = _make_pool(conn)
    update = make_telegram_update(chat_id=-1001234567890, chat_type="group")
    context = _make_context(pool, args=["validtoken"])

    await pair_command(update, context)

    pool.acquire.assert_not_called()  # no DB work → no upsert, token not consumed
    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert "1:1" in text or "private chat" in text.lower()


@pytest.mark.asyncio
async def test_pair_in_private_chat_still_succeeds():
    """Regression: a valid /pair in a private chat completes and replies Paired."""
    conn = MagicMock()
    # 1st fetchrow = token lookup; 2nd fetchrow = upsert RETURNING row.
    conn.fetchrow = AsyncMock(
        side_effect=[
            {
                "user_id": 5,
                "expires_at": datetime.now(UTC) + timedelta(minutes=10),
                "consumed_at": None,
            },
            {"was_update": False, "prior_chat_id": None},
        ]
    )
    conn.execute = AsyncMock(return_value="EXECUTE 1")
    conn.transaction = MagicMock(return_value=FakeTxnCM())
    pool = _make_pool(conn)
    update = make_telegram_update(chat_id=424242, chat_type="private")
    context = _make_context(pool, args=["validtoken"])

    await pair_command(update, context)

    pool.acquire.assert_called_once()  # private chat reaches the DB flow
    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert "Paired" in text


# test_pair_expired_token_deletes_and_replies_error — deleted; DB assertion covered by
#   test_tg_contract.py::test_pair_command_rejects_expired_token
# Kept: test_pair_rebound_emits_system_event_and_notifies_prior_chat (unique audit path)

# test_pair_valid_token_upserts_pairing_and_marks_consumed — deleted; DB persistence covered by
#   services/telegram_bot/tests/contract/test_tg_contract.py::test_pair_command_persists_pairing


# ---------------------------------------------------------------------------
# /unpair tests
# ---------------------------------------------------------------------------


# test_unpair_paired_chat_removes_pairing — deleted; DB deletion covered by
#   services/telegram_bot/tests/contract/test_tg_contract.py::test_unpair_command_deletes_pairing


@pytest.mark.asyncio
async def test_unpair_not_paired_chat_replies_informational():
    """Unpair when the DELETE removes 0 rows gives an informational reply.

    The chat is authorised (auth_check finds a pairing row via pool.fetchrow),
    but the in-transaction DELETE returns "DELETE 0" — e.g. a race where the
    row vanished — so the handler reports no active pairing.
    """
    conn = _make_conn(fetchval_return=None, execute_return="DELETE 0")
    # auth_check (decorator) uses pool.fetchrow → must find a pairing row so the
    # chat is authorised and the handler body runs.
    pool = _make_pool(conn, fetchrow_return={"user_id": 1})
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


# test_whoami_paired_chat_shows_paired_since — deleted; real DB read covered by
#   services/telegram_bot/tests/contract/test_tg_contract.py::test_whoami_command_reads_real_pairing
# Note: DB PK leak assertion retained in test_whoami_does_not_leak_db_user_id below.


@pytest.mark.asyncio
async def test_whoami_unpaired_chat_shows_instructions():
    """Unpaired chat shows how to pair (no legacy-owner branch)."""
    pool = _make_pool(_make_conn(), fetchrow_return=None)
    update = make_telegram_update(chat_id=99999)
    config = _make_config(telegram_chat_id=777)
    context = _make_context(pool, config=config, args=[])

    await whoami_command(update, context)

    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert "not paired" in text.lower() or "pair" in text.lower()


@pytest.mark.asyncio
async def test_whoami_unpaired_even_when_chat_matches_env_var():
    """A chat whose id matches the legacy env-var still reports 'not paired'
    when there is no telegram_user_pairings row — the legacy-owner branch is
    retired."""
    pool = _make_pool(_make_conn(), fetchrow_return=None)
    update = make_telegram_update(chat_id=777)
    config = _make_config(telegram_chat_id=777)
    context = _make_context(pool, config=config, args=[])

    await whoami_command(update, context)

    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert "not paired" in text.lower()
    assert "system owner" not in text.lower()
    assert "legacy" not in text.lower()


# ---------------------------------------------------------------------------
# /pair rebound — second pairing from a new chat emits audit + notify
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pair_rebound_emits_system_event_and_notifies_prior_chat():
    """Pairing from a new chat displacing an existing pairing must:
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
    """If notifying prior chat raises (stale/blocked), pairing still succeeds."""
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
# /whoami must not leak raw DB user_id (PK)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_whoami_does_not_leak_db_user_id():
    """/whoami reply must contain no 'user_id=<digits>' pattern."""
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
    assert not re.search(r"user_id=\d+", text), f"raw DB PK leaked in /whoami reply: {text!r}"
    assert "12345678" not in text, f"raw numeric PK 12345678 leaked in /whoami reply: {text!r}"


# ---------------------------------------------------------------------------
# M12c: /whoami must HTML-escape telegram_username
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_whoami_escapes_username_angle_bracket():
    """M12c: username containing '<' must be HTML-escaped in the reply."""
    paired_at = datetime.now(UTC)
    row = {
        "user_id": 1,
        "telegram_username": "<script>",
        "paired_at": paired_at,
    }
    pool = _make_pool(_make_conn(), fetchrow_return=row)
    update = make_telegram_update(chat_id=42)
    context = _make_context(pool, args=[])

    await whoami_command(update, context)

    text = update.message.reply_text.call_args[0][0]
    assert "<script>" not in text, (
        f"M12c: raw '<script>' must not appear in /whoami reply: {text!r}"
    )
    assert "&lt;script&gt;" in text, (
        f"M12c: escaped '&lt;script&gt;' must appear in /whoami reply: {text!r}"
    )


@pytest.mark.asyncio
async def test_whoami_escapes_username_ampersand():
    """M12c: username containing '&' must be HTML-escaped in the reply."""
    paired_at = datetime.now(UTC)
    row = {
        "user_id": 2,
        "telegram_username": "AT&T",
        "paired_at": paired_at,
    }
    pool = _make_pool(_make_conn(), fetchrow_return=row)
    update = make_telegram_update(chat_id=43)
    context = _make_context(pool, args=[])

    await whoami_command(update, context)

    text = update.message.reply_text.call_args[0][0]
    assert "AT&T" not in text, f"M12c: raw '&' must not appear in /whoami reply: {text!r}"
    assert "AT&amp;T" in text, f"M12c: escaped 'AT&amp;T' must appear in /whoami reply: {text!r}"


@pytest.mark.asyncio
async def test_whoami_plain_username_unaffected():
    """M12c: a username with no HTML-special chars renders unchanged."""
    paired_at = datetime.now(UTC)
    row = {
        "user_id": 3,
        "telegram_username": "alice_bob",
        "paired_at": paired_at,
    }
    pool = _make_pool(_make_conn(), fetchrow_return=row)
    update = make_telegram_update(chat_id=44)
    context = _make_context(pool, args=[])

    await whoami_command(update, context)

    text = update.message.reply_text.call_args[0][0]
    assert "alice_bob" in text, (
        f"M12c: plain username 'alice_bob' must appear unchanged in /whoami reply: {text!r}"
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
