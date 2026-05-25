"""Tests for /start handler (SEC-RC-1): oversized pairing code rejection.

Verified:
  services/telegram_bot/telegram_bot/handlers/commands/system_commands.py:143
    — await _handle_pairing(update, context, code)  [guarded by len(code) > 64 check at line 140]
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jarvis_common.testing import make_bot_config, make_telegram_update
from telegram_bot.config import BotConfig


def _make_context(pool=None, config=None):
    if config is None:
        config = make_bot_config(BotConfig, telegram_chat_id=None)
    if pool is None:
        pool = MagicMock()
    context = MagicMock()
    context.application = MagicMock()
    context.application.bot_data = {
        "config": config,
        "db_pool": pool,
        "http_client": AsyncMock(),
    }
    return context


@pytest.mark.asyncio
async def test_start_rejects_oversized_pairing_code() -> None:
    """PAIR_ codes longer than 64 chars after the prefix must be dropped and user notified."""
    from telegram_bot.handlers.commands.system_commands import start_command

    oversized_code = "PAIR_" + "A" * 100  # 105 chars total; 100-char body exceeds 64 limit
    update = make_telegram_update(chat_id=1, text=f"/start {oversized_code}")
    context = _make_context()

    with patch(
        "telegram_bot.handlers.commands.system_commands._handle_pairing",
        new_callable=AsyncMock,
    ) as mock_pair:
        await start_command(update, context)
        mock_pair.assert_not_called()
        update.message.reply_text.assert_awaited_once_with(
            "This pairing link is invalid. Please request a new one."
        )


@pytest.mark.asyncio
async def test_start_accepts_valid_length_pairing_code() -> None:
    """PAIR_ codes at or below 64 chars must still reach _handle_pairing."""
    from telegram_bot.handlers.commands.system_commands import start_command

    # 43 chars is the real base64url length for a 32-byte secret — well within the limit
    valid_code = "PAIR_" + "B" * 43
    update = make_telegram_update(chat_id=1, text=f"/start {valid_code}")
    context = _make_context()

    with patch(
        "telegram_bot.handlers.commands.system_commands._handle_pairing",
        new_callable=AsyncMock,
    ) as mock_pair:
        await start_command(update, context)
        mock_pair.assert_awaited_once_with(update, context, "B" * 43)


@pytest.mark.asyncio
async def test_start_accepts_exactly_64_char_code() -> None:
    """PAIR_ codes exactly 64 chars long are at the boundary and must pass through."""
    from telegram_bot.handlers.commands.system_commands import start_command

    boundary_code = "PAIR_" + "C" * 64
    update = make_telegram_update(chat_id=1, text=f"/start {boundary_code}")
    context = _make_context()

    with patch(
        "telegram_bot.handlers.commands.system_commands._handle_pairing",
        new_callable=AsyncMock,
    ) as mock_pair:
        await start_command(update, context)
        mock_pair.assert_awaited_once_with(update, context, "C" * 64)


@pytest.mark.asyncio
async def test_start_rejects_65_char_code() -> None:
    """PAIR_ codes exactly 65 chars long are just over the boundary and must be rejected."""
    from telegram_bot.handlers.commands.system_commands import start_command

    over_boundary_code = "PAIR_" + "D" * 65
    update = make_telegram_update(chat_id=1, text=f"/start {over_boundary_code}")
    context = _make_context()

    with patch(
        "telegram_bot.handlers.commands.system_commands._handle_pairing",
        new_callable=AsyncMock,
    ) as mock_pair:
        await start_command(update, context)
        mock_pair.assert_not_called()
