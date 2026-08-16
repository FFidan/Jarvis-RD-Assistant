"""Tests for Platform-gated /start and /pair rate limiting."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from jarvis_common.testing import (
    PTBContextOptions,
    make_bot_config,
    make_ptb_context,
    make_telegram_update,
)
from jarvis_common.testing_telegram import make_http_response
from telegram_bot.config import BotConfig
from telegram_bot.handlers.commands import start_command
from telegram_bot.handlers.commands.pairing_commands import pair_command


def _make_config() -> BotConfig:
    """Return the standard database-free bot test configuration."""
    return make_bot_config(BotConfig)


@pytest.mark.asyncio
async def test_start_unpaired_chat_shows_pair_guidance() -> None:
    """An unpaired chat receives pairing guidance and no welcome text."""
    platform_client = AsyncMock()
    update = make_telegram_update(chat_id=999, text="/start")
    context = make_ptb_context(
        platform_client,
        _make_config(),
        options=PTBContextOptions(paired_user_id=None),
    )

    await start_command(update, context)

    text = update.message.reply_text.await_args.args[0]
    assert "/pair" in text
    assert "Welcome" not in text


@pytest.mark.asyncio
async def test_start_paired_chat_sends_welcome() -> None:
    """A Platform-paired chat receives the welcome message."""
    platform_client = AsyncMock()
    update = make_telegram_update(chat_id=777, text="/start")
    context = make_ptb_context(platform_client, _make_config())

    await start_command(update, context)

    assert "Welcome" in update.message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_pair_command_rate_limited_after_five_calls() -> None:
    """The sixth pairing attempt in one minute never reaches Platform."""
    from telegram_bot.handlers.rate_limit import _timestamps

    _timestamps.clear()
    platform_client = AsyncMock()
    platform_client.post = AsyncMock(
        return_value=make_http_response(
            {
                "outcome": "paired",
                "user_id": 1,
                "prior_chat_id": None,
            }
        )
    )
    config = _make_config()
    results: list[object] = []

    for _ in range(6):
        update = make_telegram_update(chat_id=555_001, text="/pair abc123")
        context = make_ptb_context(
            platform_client,
            config,
            options=PTBContextOptions(args=["abc123"]),
        )
        results.append(await pair_command(update, context))

    assert results[5] is None
    assert platform_client.post.await_count == 5
