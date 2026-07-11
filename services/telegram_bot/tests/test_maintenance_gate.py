"""Tests for the telegram maintenance gate (bot bypasses the HTTP middleware).

The bot has no MaintenanceMiddleware, so ``_maintenance_gate`` — a TypeHandler
registered in group -1 — is its restore guard: while a sentinel is fresh it
replies once and raises ``ApplicationHandlerStop`` so no downstream handler
writes to the being-restored database; with no sentinel it passes through.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.ext import ApplicationHandlerStop, TypeHandler


def _point_sentinels(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MAINTENANCE_SENTINEL", str(tmp_path / ".maintenance"))
    monkeypatch.setenv("MAINTENANCE_DESTRUCTIVE_SENTINEL", str(tmp_path / ".destructive"))


@pytest.mark.asyncio
async def test_gate_replies_and_stops_handling_under_sentinel(tmp_path, monkeypatch) -> None:
    from telegram_bot.main import _maintenance_gate

    _point_sentinels(tmp_path, monkeypatch)
    (tmp_path / ".maintenance").touch()

    message = MagicMock()
    message.reply_text = AsyncMock()
    update = MagicMock()
    update.callback_query = None
    update.effective_message = message

    with pytest.raises(ApplicationHandlerStop):
        await _maintenance_gate(update, MagicMock())

    message.reply_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_gate_answers_callback_query_under_sentinel(tmp_path, monkeypatch) -> None:
    from telegram_bot.main import _maintenance_gate

    _point_sentinels(tmp_path, monkeypatch)
    (tmp_path / ".maintenance").touch()

    callback = MagicMock()
    callback.answer = AsyncMock()
    update = MagicMock()
    update.callback_query = callback

    with pytest.raises(ApplicationHandlerStop):
        await _maintenance_gate(update, MagicMock())

    callback.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_gate_passes_through_when_no_sentinel(tmp_path, monkeypatch) -> None:
    from telegram_bot.main import _maintenance_gate

    _point_sentinels(tmp_path, monkeypatch)  # both absent → inactive

    message = MagicMock()
    message.reply_text = AsyncMock()
    update = MagicMock()
    update.callback_query = None
    update.effective_message = message

    # No sentinel → returns without replying or raising.
    await _maintenance_gate(update, MagicMock())
    message.reply_text.assert_not_called()


def test_main_registers_gate_in_group_minus_one() -> None:
    """main() installs the maintenance gate as a group -1 TypeHandler (runs first)."""
    with (
        patch("telegram_bot.main.reload_fernet_on_sighup"),
        patch("telegram_bot.main.BotConfig.from_env", return_value=MagicMock()),
        patch("telegram_bot.main.Application.builder") as mock_builder,
    ):
        mock_app = MagicMock()
        chain = mock_builder.return_value.token.return_value.post_init.return_value
        chain.post_shutdown.return_value.build.return_value = mock_app

        from telegram_bot.main import _maintenance_gate, main

        main()

        group_minus_one = [
            call for call in mock_app.add_handler.call_args_list if call.kwargs.get("group") == -1
        ]
        assert len(group_minus_one) == 1, "exactly one handler must be registered in group -1"
        handler = group_minus_one[0].args[0]
        assert isinstance(handler, TypeHandler)
        assert handler.callback is _maintenance_gate
