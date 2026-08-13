"""Tests for the /start handler.

Pairing (``telegram_user_pairings``) is the sole bot-identity mechanism; the
legacy ``/start PAIR_<code>`` deep-link has been retired. ``/start`` now simply
goes through :func:`auth_check`:
- a paired chat receives the welcome message
- an unpaired chat receives the /pair guidance
"""

from __future__ import annotations

from functools import partial

import pytest
from jarvis_common.testing import (
    make_bot_config,
    make_pool_and_conn,
    make_ptb_context,
    make_telegram_update,
)
from telegram_bot.config import BotConfig
from telegram_bot.handlers.commands.system_commands import start_command


def _make_pool(*, pairing_row=None):
    return make_pool_and_conn(fetchrow_return=pairing_row, direct_methods=True)[0]


_make_context = partial(
    make_ptb_context,
    config=make_bot_config(BotConfig),
)


@pytest.mark.asyncio
async def test_start_paired_chat_sends_welcome() -> None:
    """A paired chat (pairing row exists) gets the welcome message."""
    pool = _make_pool(pairing_row={"user_id": 1})
    update = make_telegram_update(chat_id=42, text="/start")
    context = _make_context(pool)

    await start_command(update, context)

    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert "Welcome" in text


@pytest.mark.asyncio
async def test_start_unpaired_chat_shows_pair_guidance() -> None:
    """An unpaired chat (no pairing row) gets the /pair guidance, not welcome."""
    pool = _make_pool(pairing_row=None)
    update = make_telegram_update(chat_id=42, text="/start")
    context = _make_context(pool)

    await start_command(update, context)

    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert "/pair" in text
    assert "Welcome" not in text
