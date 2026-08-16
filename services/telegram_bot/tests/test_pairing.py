"""Telegram pairing command contracts through the scoped Platform API."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from jarvis_common.testing import (
    PTBContextOptions,
    make_bot_config,
    make_ptb_context,
    make_telegram_update,
)
from telegram.constants import ChatType
from telegram_bot.config import BotConfig
from telegram_bot.handlers.commands.pairing_commands import (
    pair_command,
    unpair_command,
    whoami_command,
)
from telegram_bot.platform_client import PairingOutcome, UserPairing


def _context(*, args: list[str] | None = None):
    platform_client = AsyncMock()
    config = make_bot_config(BotConfig)
    context = make_ptb_context(
        platform_client,
        config,
        options=PTBContextOptions(args=args, with_bot=True),
    )
    return context


@pytest.mark.asyncio
async def test_pair_no_args_replies_usage() -> None:
    update = make_telegram_update()
    context = _context(args=[])

    await pair_command(update, context)

    assert "/pair" in update.message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_pair_group_chat_is_rejected_before_platform_call() -> None:
    update = make_telegram_update(chat_type=ChatType.GROUP)
    context = _context(args=["pairing-code"])

    with patch("telegram_bot.handlers.commands.pairing_commands.pair_chat", AsyncMock()) as call:
        await pair_command(update, context)

    call.assert_not_awaited()
    assert "direct" in update.message.reply_text.await_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "expected"),
    [("invalid", "Invalid"), ("used", "already"), ("expired", "expired")],
)
async def test_pair_surfaces_stable_platform_outcomes(outcome: str, expected: str) -> None:
    update = make_telegram_update()
    context = _context(args=["pairing-code"])
    result = PairingOutcome(outcome=outcome)  # type: ignore[arg-type]

    with patch(
        "telegram_bot.handlers.commands.pairing_commands.pair_chat",
        AsyncMock(return_value=result),
    ):
        await pair_command(update, context)

    assert expected in update.message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_pair_success_notifies_displaced_chat() -> None:
    update = make_telegram_update(chat_id=22, username="new-user")
    context = _context(args=["pairing-code"])
    result = PairingOutcome(outcome="paired", user_id=7, prior_chat_id=11)

    with patch(
        "telegram_bot.handlers.commands.pairing_commands.pair_chat",
        AsyncMock(return_value=result),
    ) as call:
        await pair_command(update, context)

    call.assert_awaited_once_with(
        context.application.bot_data["platform_client"],
        context.application.bot_data["config"],
        token="pairing-code",
        chat_id=22,
        telegram_username="new-user",
    )
    context.bot.send_message.assert_awaited_once()
    assert context.bot.send_message.await_args.args[0] == 11
    assert "Paired" in update.message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_pair_stale_displaced_chat_does_not_fail_new_pairing() -> None:
    update = make_telegram_update(chat_id=22)
    context = _context(args=["pairing-code"])
    context.bot.send_message.side_effect = RuntimeError("blocked")
    result = PairingOutcome(outcome="paired", user_id=7, prior_chat_id=11)

    with patch(
        "telegram_bot.handlers.commands.pairing_commands.pair_chat",
        AsyncMock(return_value=result),
    ):
        await pair_command(update, context)

    assert "Paired" in update.message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_pair_conflicting_chat_surfaces_clear_error() -> None:
    update = make_telegram_update(chat_id=22)
    context = _context(args=["pairing-code"])
    request = httpx.Request("POST", "http://platform/internal/telegram/pairings")
    response = httpx.Response(409, request=request)

    with patch(
        "telegram_bot.handlers.commands.pairing_commands.pair_chat",
        AsyncMock(
            side_effect=httpx.HTTPStatusError("conflict", request=request, response=response)
        ),
    ):
        await pair_command(update, context)

    assert "another account" in update.message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_unpair_is_idempotent_through_platform() -> None:
    update = make_telegram_update(chat_id=22)
    context = _context()

    with (
        patch(
            "telegram_bot.handlers.commands._auth.auth_check",
            AsyncMock(return_value=(True, 7)),
        ),
        patch(
            "telegram_bot.handlers.commands.pairing_commands.unpair_chat",
            AsyncMock(return_value=False),
        ) as call,
    ):
        await unpair_command(update, context)

    call.assert_awaited_once()
    assert "No active pairing" in update.message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_whoami_unpaired_shows_instructions() -> None:
    update = make_telegram_update(chat_id=22)
    context = _context()

    with patch(
        "telegram_bot.handlers.commands.pairing_commands.resolve_pairing",
        AsyncMock(return_value=None),
    ):
        await whoami_command(update, context)

    assert "not paired" in update.message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_whoami_escapes_username_and_hides_user_id() -> None:
    update = make_telegram_update(chat_id=22)
    context = _context()
    pairing = UserPairing(
        user_id=987654,
        chat_id=22,
        telegram_username="alice<&",
        paired_at="2026-08-16T10:30:00+00:00",
    )

    with patch(
        "telegram_bot.handlers.commands.pairing_commands.resolve_pairing",
        AsyncMock(return_value=pairing),
    ):
        await whoami_command(update, context)

    text = update.message.reply_text.await_args.args[0]
    assert "alice&lt;&amp;" in text
    assert "987654" not in text


@pytest.mark.asyncio
async def test_pairing_platform_outage_fails_closed() -> None:
    update = make_telegram_update(chat_id=22)
    context = _context(args=["pairing-code"])

    with patch(
        "telegram_bot.handlers.commands.pairing_commands.pair_chat",
        AsyncMock(side_effect=httpx.ConnectError("offline")),
    ):
        await pair_command(update, context)

    assert "failed" in update.message.reply_text.await_args.args[0]
