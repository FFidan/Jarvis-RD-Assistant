"""Tests for the /start handler.

Pairing (``telegram_user_pairings``) is the sole bot-identity mechanism; the
legacy ``/start PAIR_<code>`` deep-link has been retired. ``/start`` now simply
goes through :func:`auth_check`:
- a paired chat receives the welcome message
- an unpaired chat receives the /pair guidance
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from jarvis_common.testing import make_bot_config, make_telegram_update
from telegram_bot.config import BotConfig
from telegram_bot.handlers.commands.system_commands import start_command


def _make_pool(*, pairing_row=None):
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value=pairing_row)
    return pool


def _make_context(pool, config=None):
    if config is None:
        config = make_bot_config(BotConfig, telegram_chat_id=None)
    context = MagicMock()
    context.application = MagicMock()
    context.application.bot_data = {
        "config": config,
        "db_pool": pool,
        "http_client": AsyncMock(),
    }
    return context


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
