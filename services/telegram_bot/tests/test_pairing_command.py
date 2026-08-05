"""Tests for /start (pairing-gated) and /pair rate-limiting.

Pairing (``telegram_user_pairings``) is the sole bot-identity mechanism, so:
- /start from a paired chat   -> welcome message
- /start from an unpaired chat -> /pair guidance (no welcome)
- /pair                        -> rate-limited at 5 calls / 60 s per chat
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from functools import partial
from unittest.mock import AsyncMock

import pytest
from jarvis_common.testing import (
    make_bot_config,
    make_conn,
    make_pool_and_conn,
    make_telegram_update,
)
from jarvis_common.testing import (
    make_ptb_context as _make_context,
)
from telegram_bot.config import BotConfig
from telegram_bot.handlers.commands import start_command  # noqa: E402
from telegram_bot.handlers.commands.pairing_commands import pair_command  # noqa: E402

_PAIRED_CHAT_ID = 777


def _make_config(telegram_chat_id: int | None = None):
    return make_bot_config(BotConfig, telegram_chat_id=telegram_chat_id)


_make_conn = partial(
    make_conn,
    fetchrow_return=None,
    fetchval_return=None,
    execute_return="EXECUTE 1",
)


def _make_pool(conn, *, fetchrow_return=None):
    # conn is used as-is (with_transaction=False); the pool-level pairing lookup
    # intentionally returns a different row than the conn-level command flow.
    pool, _conn = make_pool_and_conn(conn=conn, with_transaction=False)
    pool.fetchrow = AsyncMock(return_value=fetchrow_return)
    return pool


# ---------------------------------------------------------------------------
# /start — pairing-gated welcome / guidance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_unpaired_chat_shows_pair_guidance():
    """Plain /start from an unpaired chat replies with /pair guidance, no welcome."""
    pool = _make_pool(_make_conn(), fetchrow_return=None)  # no pairing row
    update = make_telegram_update(chat_id=999, text="/start")
    context = _make_context(pool, _make_config())

    await start_command(update, context)

    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert "/pair" in text
    assert "Welcome" not in text


@pytest.mark.asyncio
async def test_start_paired_chat_sends_welcome():
    """Plain /start from a paired chat sends the welcome message."""
    pool = _make_pool(_make_conn(), fetchrow_return={"user_id": 1})
    update = make_telegram_update(chat_id=_PAIRED_CHAT_ID, text="/start")
    context = _make_context(pool, _make_config())

    await start_command(update, context)

    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert "Welcome" in text


# ---------------------------------------------------------------------------
# /pair rate limit (5/minute per chat)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pair_command_rate_limited_after_five_calls() -> None:
    """pair_command is decorated with @rate_limit(max_calls=5, window_seconds=60).

    Rapid-fire calls from the same chat_id must be silently dropped (return
    None) after the 5th call.  The DB pool is configured to always return a
    valid unexpired token so the handler would succeed without the rate limit.
    """
    from telegram_bot.handlers.rate_limit import _timestamps

    _timestamps.clear()

    future = datetime.now(UTC) + timedelta(minutes=5)
    conn = _make_conn(fetchrow_return={"user_id": 1, "expires_at": future, "consumed_at": None})
    pool = _make_pool(conn)
    config = _make_config()

    chat_id = 555_001
    results: list = []
    for _ in range(6):
        update = make_telegram_update(chat_id=chat_id, text="/pair abc123")
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
