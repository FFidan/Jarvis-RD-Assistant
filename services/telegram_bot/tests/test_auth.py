"""Regression tests for auth_check — telegram_user_pairings is the sole identity.

The 2-valued contract:
- (True, real_user_id) when a telegram_user_pairings row exists for the chat_id
- (False, None) otherwise (no chat / DB error / no pairing row)

Invariant: authorized is True ⟺ user_id is not None.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from jarvis_common.testing import (
    make_bot_config,
    make_pool_and_conn,
    make_ptb_context,
    make_telegram_update,
)
from telegram_bot.config import BotConfig
from telegram_bot.handlers.commands._auth import auth_required
from telegram_bot.handlers.helpers import auth_check as _auth_check


def _make_pool(*, fetchrow_return=None) -> MagicMock:
    """Build a minimal pool mock covering the telegram_user_pairings lookup."""
    return make_pool_and_conn(fetchrow_return=fetchrow_return, direct_methods=True)[0]


# ---------------------------------------------------------------------------
# auth_check — pairing-only contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_check_accepts_paired_user():
    """chat_id present in telegram_user_pairings grants access with its user_id."""
    pairing_row = {"user_id": 1}
    pool = _make_pool(fetchrow_return=pairing_row)
    update = make_telegram_update(chat_id=999)
    config = make_bot_config(BotConfig, telegram_chat_id=None)

    authorized, user_id = await _auth_check(update, config, pool)

    assert authorized is True
    assert user_id == 1
    # Ensure the pairing query was actually made
    pool.fetchrow.assert_awaited_once()
    call_sql = pool.fetchrow.await_args.args[0]
    assert "telegram_user_pairings" in call_sql
    assert pool.fetchrow.await_args.args[1] == 999


@pytest.mark.asyncio
async def test_auth_check_rejects_unpaired_unknown_chat():
    """chat_id absent from the pairings table -> (False, None)."""
    pool = _make_pool(fetchrow_return=None)
    update = make_telegram_update(chat_id=12345)
    config = make_bot_config(BotConfig, telegram_chat_id=None)

    authorized, user_id = await _auth_check(update, config, pool)

    assert authorized is False
    assert user_id is None
    pool.fetchrow.assert_awaited_once()


@pytest.mark.asyncio
async def test_auth_check_returns_user_id_for_paired_chat():
    """Paired chats expose the DB user_id for downstream scoping."""
    pool = _make_pool(fetchrow_return={"user_id": 7})
    update = make_telegram_update(chat_id=999)
    config = make_bot_config(BotConfig, telegram_chat_id=None)

    assert await _auth_check(update, config, pool) == (True, 7)


@pytest.mark.asyncio
async def test_auth_check_ignores_env_var_owner():
    """A chat matching the legacy env-var TELEGRAM_CHAT_ID is NOT authorised
    unless it also has a pairing row — the env-var path is retired."""
    pool = _make_pool(fetchrow_return=None)
    update = make_telegram_update(chat_id=12345)
    config = make_bot_config(BotConfig, telegram_chat_id=12345)

    assert await _auth_check(update, config, pool) == (False, None)


@pytest.mark.asyncio
async def test_auth_check_db_error_denies():
    """A DB error during the pairing lookup denies the request (fail-closed)."""
    pool = MagicMock()
    pool.fetchrow = AsyncMock(side_effect=RuntimeError("db down"))
    update = make_telegram_update(chat_id=999)
    config = make_bot_config(BotConfig, telegram_chat_id=None)

    assert await _auth_check(update, config, pool) == (False, None)


@pytest.mark.asyncio
async def test_auth_check_denies_non_private_chat_even_when_paired():
    """A group/supergroup chat is denied WITHOUT resolving identity, even when a
    stale telegram_user_pairings row exists for its (negative) chat_id.

    Identity binds to chat_id, so a group pairing would let every member act as
    the paired user.  The gate must fire before the DB lookup so stale group
    pairings become inert for authed ops.
    """
    pool = _make_pool(fetchrow_return={"user_id": 1})  # stale group pairing present
    update = make_telegram_update(chat_id=-1001234567890, chat_type="group")
    config = make_bot_config(BotConfig, telegram_chat_id=None)

    assert await _auth_check(update, config, pool) == (False, None)
    pool.fetchrow.assert_not_awaited()  # identity never resolved for a group chat


# ---------------------------------------------------------------------------
# @auth_required — unpaired callers get the /pair reply and the handler is skipped
# ---------------------------------------------------------------------------


def _make_decorator_context() -> MagicMock:
    pool, _conn = make_pool_and_conn(
        fetchrow_return=None,  # unpaired
        fetchval_return=None,
        execute_return="OK",
        direct_methods=True,
    )
    return make_ptb_context(pool, make_bot_config(BotConfig, telegram_chat_id=None))


@pytest.mark.asyncio
async def test_auth_required_unpaired_replies_pair_guidance_and_skips_handler():
    """S6: an unpaired user gets the /pair guidance reply and the wrapped
    handler is NOT invoked."""
    called: list[bool] = []

    @auth_required
    async def _handler(update, context):
        called.append(True)

    update = make_telegram_update(chat_id=4242, text="/help")
    context = _make_decorator_context()

    await _handler(update, context)

    assert called == [], "wrapped handler must not run for an unpaired chat"
    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert "/pair" in text


@pytest.mark.asyncio
async def test_auth_required_paired_runs_handler_and_stashes_user_id():
    """A paired chat runs the handler and stashes the real user_id."""
    seen: list[int | None] = []

    @auth_required
    async def _handler(update, context):
        seen.append(context.user_data.get("jarvis_user_id"))

    update = make_telegram_update(chat_id=4242, text="/help")
    context = _make_decorator_context()
    context.application.bot_data["db_pool"].fetchrow = AsyncMock(return_value={"user_id": 99})

    await _handler(update, context)

    assert seen == [99]


@pytest.mark.asyncio
async def test_auth_required_denies_group_chat_even_when_paired():
    """An authed op invoked in a group chat is denied and the handler is skipped,
    even when a telegram_user_pairings row exists for that chat_id."""
    called: list[bool] = []

    @auth_required
    async def _handler(update, context):
        called.append(True)

    update = make_telegram_update(chat_id=-1001234567890, chat_type="group", text="/help")
    context = _make_decorator_context()
    context.application.bot_data["db_pool"].fetchrow = AsyncMock(return_value={"user_id": 99})

    await _handler(update, context)

    assert called == [], "authed handler must not run in a group chat"
    update.message.reply_text.assert_awaited_once()
