"""Telegram restore-pause and outbound-quarantine tests.

The first handler stops downstream work during maintenance. Quarantine is
stricter: it suppresses both polling and the bot's own denial message so restored
Telegram credentials cannot be used before review.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, call, patch

import httpx
import pytest
from jarvis_common.maintenance import OutboundEgressBlockedError
from telegram.ext import ApplicationHandlerStop, TypeHandler


def _point_sentinels(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MAINTENANCE_SENTINEL", str(tmp_path / ".maintenance"))
    monkeypatch.setenv("MAINTENANCE_DESTRUCTIVE_SENTINEL", str(tmp_path / ".destructive"))
    monkeypatch.setenv("OUTBOUND_QUARANTINE_SENTINEL", str(tmp_path / ".outbound-quarantine.json"))


def _configure_fluent_builder(builder: MagicMock, app: MagicMock) -> None:
    """Configure the PTB builder double to return itself at each step."""
    builder.request.return_value = builder
    builder.get_updates_request.return_value = builder
    builder.token.return_value = builder
    builder.post_init.return_value = builder
    builder.post_shutdown.return_value = builder
    builder.build.return_value = app


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


@pytest.mark.asyncio
async def test_gate_stops_silently_during_outbound_quarantine(tmp_path, monkeypatch) -> None:
    """A quarantined bot must not send even its own denial message."""
    from telegram_bot.main import _maintenance_gate

    _point_sentinels(tmp_path, monkeypatch)
    (tmp_path / ".outbound-quarantine.json").touch()
    message = MagicMock()
    message.reply_text = AsyncMock()
    update = MagicMock(callback_query=None, effective_message=message)

    with pytest.raises(ApplicationHandlerStop):
        await _maintenance_gate(update, MagicMock())

    message.reply_text.assert_not_awaited()


def test_main_registers_gate_in_group_minus_one() -> None:
    """main() installs the maintenance gate as a group -1 TypeHandler (runs first)."""
    with (
        patch("telegram_bot.main.BotConfig.from_env", return_value=MagicMock()),
        patch("telegram_bot.main.Application.builder") as mock_builder,
    ):
        mock_app = MagicMock()
        _configure_fluent_builder(mock_builder.return_value, mock_app)

        from telegram_bot.main import _maintenance_gate, main

        main()

        group_minus_one = [
            call for call in mock_app.add_handler.call_args_list if call.kwargs.get("group") == -1
        ]
        assert len(group_minus_one) == 1, "exactly one handler must be registered in group -1"
        handler = group_minus_one[0].args[0]
        assert isinstance(handler, TypeHandler)
        assert handler.callback is _maintenance_gate


def test_main_does_not_load_token_or_start_polling_during_quarantine(tmp_path, monkeypatch) -> None:
    """Startup quarantine is checked before restored Telegram credentials load."""
    from telegram_bot.main import main

    _point_sentinels(tmp_path, monkeypatch)
    (tmp_path / ".outbound-quarantine.json").touch()
    with (
        patch("telegram_bot.main.BotConfig.from_env") as config,
        patch("telegram_bot.main.Application.builder") as builder,
    ):
        main()

    config.assert_not_called()
    builder.assert_not_called()


@pytest.mark.asyncio
async def test_telegram_transport_refuses_quarantine_before_http(tmp_path, monkeypatch) -> None:
    """The Telegram transport blocks a request before the HTTP transport runs."""
    from telegram_bot.main import _QuarantineAwareHTTPXRequest

    _point_sentinels(tmp_path, monkeypatch)
    (tmp_path / ".outbound-quarantine.json").touch()
    requests = 0

    def handle_request(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json={"ok": True, "result": {}})

    request = _QuarantineAwareHTTPXRequest(
        httpx_kwargs={"transport": httpx.MockTransport(handle_request)}
    )
    await request.initialize()
    try:
        with pytest.raises(OutboundEgressBlockedError, match="credential review"):
            await request.do_request("https://api.telegram.org/botredacted/getMe", "POST")
    finally:
        await request.shutdown()

    assert requests == 0


def test_main_wires_quarantine_transport_to_both_telegram_channels() -> None:
    """Polling and ordinary Bot API traffic use separate guarded transports."""
    normal_request = object()
    updates_request = object()
    with (
        patch("telegram_bot.main.BotConfig.from_env", return_value=MagicMock()),
        patch("telegram_bot.main.Application.builder") as application_builder,
        patch(
            "telegram_bot.main._QuarantineAwareHTTPXRequest",
            side_effect=[normal_request, updates_request],
        ) as request_type,
    ):
        builder = application_builder.return_value
        mock_app = MagicMock()
        _configure_fluent_builder(builder, mock_app)

        from telegram_bot.main import main

        main()

    assert request_type.call_args_list == [call(), call(connection_pool_size=1)]
    builder.request.assert_called_once_with(normal_request)
    builder.get_updates_request.assert_called_once_with(updates_request)
