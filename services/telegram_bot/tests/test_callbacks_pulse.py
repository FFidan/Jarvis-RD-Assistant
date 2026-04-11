"""Tests for the Pulse rating callback handlers.

Covers the three inline-keyboard buttons (Up/Down/Save) sent with each Pulse
card message: they must POST to ``/api/pulse/rate`` with the correct
``(paper_id, rating)`` body and degrade gracefully on backend errors.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# Ensure the telegram_bot app package is importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Stub heavy native modules unavailable outside Docker.
for _mod_name in (
    "telegram",
    "telegram.ext",
    "apscheduler",
    "apscheduler.schedulers",
    "apscheduler.schedulers.asyncio",
    "apscheduler.triggers",
    "apscheduler.triggers.cron",
):
    if _mod_name not in sys.modules:
        sys.modules[_mod_name] = MagicMock()

_tg = sys.modules["telegram"]
_tg.Update = MagicMock
_tg.Message = MagicMock
_tg.InlineKeyboardButton = lambda *a, **kw: MagicMock()
_tg.InlineKeyboardMarkup = lambda *a, **kw: MagicMock()

_tg_ext = sys.modules["telegram.ext"]
_tg_ext.Application = MagicMock
_tg_ext.CommandHandler = MagicMock
_tg_ext.CallbackQueryHandler = MagicMock
_tg_ext.ContextTypes = MagicMock()
_tg_ext.ContextTypes.DEFAULT_TYPE = MagicMock
_tg_ext.ConversationHandler = MagicMock()
_tg_ext.ConversationHandler.END = -1

from app.config import BotConfig  # noqa: E402
from app.handlers.callback_handler import (  # noqa: E402
    pulse_rating_callback,
)

_TEST_CHAT_ID = 12345


def _make_config() -> BotConfig:
    return BotConfig(
        telegram_token="test-token",
        telegram_chat_id=_TEST_CHAT_ID,
        database_url="postgres://test",
        paper_ingestion_url="http://paper:8000",
        learning_engine_url="http://learn:8001",
        jarvis_api_key="test-key",
    )


def _make_update_context(callback_data: str, chat_id: int = _TEST_CHAT_ID):
    """Build mock Update + Context for a pulse rating callback."""
    update = MagicMock()
    update.effective_chat = MagicMock()
    update.effective_chat.id = chat_id

    query = MagicMock()
    query.data = callback_data
    query.answer = AsyncMock()
    # Give the query a real Message instance so the isinstance check passes.
    from telegram import Message

    query.message = MagicMock(spec=Message)
    query.message.reply_text = AsyncMock()
    update.callback_query = query

    context = MagicMock()
    mock_http = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"status": "ok"}
    mock_http.post.return_value = mock_resp

    context.application = MagicMock()
    context.application.bot_data = {
        "config": _make_config(),
        "db_pool": AsyncMock(),
        "http_client": mock_http,
    }
    return update, context, mock_http


@pytest.mark.asyncio
async def test_pulse_up_callback_posts_rating():
    update, context, mock_http = _make_update_context("pulse_up_42")

    await pulse_rating_callback(update, context)

    update.callback_query.answer.assert_awaited_once()
    mock_http.post.assert_awaited_once()
    url = mock_http.post.await_args[0][0]
    assert "/api/pulse/rate" in url
    body = mock_http.post.await_args.kwargs["json"]
    assert body == {"paper_id": 42, "rating": "up"}
    # Confirmation text in the answer
    answer_text = update.callback_query.answer.await_args.kwargs.get("text", "")
    assert answer_text  # non-empty


@pytest.mark.asyncio
async def test_pulse_down_callback_posts_rating():
    update, context, mock_http = _make_update_context("pulse_down_7")

    await pulse_rating_callback(update, context)

    body = mock_http.post.await_args.kwargs["json"]
    assert body == {"paper_id": 7, "rating": "down"}


@pytest.mark.asyncio
async def test_pulse_save_callback_posts_rating():
    update, context, mock_http = _make_update_context("pulse_save_99")

    await pulse_rating_callback(update, context)

    body = mock_http.post.await_args.kwargs["json"]
    assert body == {"paper_id": 99, "rating": "save"}


@pytest.mark.asyncio
async def test_invalid_callback_data_ignored():
    """Malformed callback_data must not raise; should answer with an error."""
    update, context, mock_http = _make_update_context("pulse_bogus_42")

    await pulse_rating_callback(update, context)

    # No POST issued — we never got a valid (rating, paper_id) pair.
    mock_http.post.assert_not_awaited()
    update.callback_query.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_api_error_in_rating_sends_error_answer():
    update, context, mock_http = _make_update_context("pulse_up_42")
    mock_http.post.side_effect = Exception("boom")

    await pulse_rating_callback(update, context)

    update.callback_query.answer.assert_awaited_once()
    answer_text = update.callback_query.answer.await_args.kwargs.get("text", "")
    assert "fail" in answer_text.lower() or "try again" in answer_text.lower()


@pytest.mark.asyncio
async def test_unauthorised_chat_ignored():
    update, context, mock_http = _make_update_context("pulse_up_42", chat_id=99999)

    await pulse_rating_callback(update, context)

    mock_http.post.assert_not_awaited()
