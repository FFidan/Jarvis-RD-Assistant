"""Tests for the /start handler.

Pairing (``telegram_user_pairings``) is the sole bot-identity mechanism; the
legacy ``/start PAIR_<code>`` deep-link has been retired. ``/start`` now simply
goes through :func:`auth_check`:
- a paired chat receives the welcome message
- an unpaired chat receives the /pair guidance
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from jarvis_common.testing import (
    PTBContextOptions,
    make_bot_config,
    make_ptb_context,
    make_telegram_update,
)
from telegram_bot.config import BotConfig
from telegram_bot.handlers.commands.system_commands import start_command


@pytest.mark.asyncio
async def test_start_paired_chat_sends_welcome() -> None:
    """A Platform-paired chat gets the welcome message."""
    platform_client = AsyncMock()
    update = make_telegram_update(chat_id=42, text="/start")
    context = make_ptb_context(
        platform_client,
        make_bot_config(BotConfig),
    )

    await start_command(update, context)

    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert "Welcome" in text


@pytest.mark.asyncio
async def test_start_unpaired_chat_shows_pair_guidance() -> None:
    """An unpaired chat gets the /pair guidance, not welcome."""
    platform_client = AsyncMock()
    update = make_telegram_update(chat_id=42, text="/start")
    context = make_ptb_context(
        platform_client,
        make_bot_config(BotConfig),
        options=PTBContextOptions(paired_user_id=None),
    )

    await start_command(update, context)

    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert "/pair" in text
    assert "Welcome" not in text
