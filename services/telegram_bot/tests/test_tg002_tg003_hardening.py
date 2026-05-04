"""Tests for TG-002 and TG-003 Telegram callback hardening.

TG-002: rate_limit decorator applied to review_handler.show_answer (10/min)
        and review_handler.rate_card (5/min).
TG-003: start_review_callback handles inaccessible Message safely instead
        of doing a bare assignment that silently casts an InaccessibleMessage.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import telegram
from pydantic import SecretStr
from telegram_bot.config import BotConfig
from telegram_bot.handlers.rate_limit import _timestamps

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TEST_CHAT_ID = 54321


def _make_config() -> BotConfig:
    return BotConfig(
        telegram_token="test-token",
        telegram_chat_id=_TEST_CHAT_ID,
        database_url="postgres://test",
        paper_ingestion_url="http://paper:8000",
        learning_engine_url="http://learn:8001",
        jarvis_api_key=SecretStr("test-key"),
    )


def _make_callback_update_and_context(callback_data: str, chat_id: int = _TEST_CHAT_ID):
    """Build mock Update + Context for callback query handlers (review flow)."""
    update = MagicMock()
    update.effective_chat = MagicMock()
    update.effective_chat.id = chat_id
    # update.message is set to a MagicMock so rate_limit can call reply_text on it.
    update.message = MagicMock()
    update.message.reply_text = AsyncMock()

    query = MagicMock()
    query.data = callback_data
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    update.callback_query = query

    context = MagicMock()
    context.user_data = {}
    config = _make_config()
    mock_http = AsyncMock()

    context.application = MagicMock()
    context.application.bot_data = {
        "config": config,
        "db_pool": AsyncMock(),
        "http_client": mock_http,
    }

    return update, context, mock_http


def _make_start_review_update_and_context(
    message_is_accessible: bool = True, chat_id: int = _TEST_CHAT_ID
):
    """Build mock Update + Context for start_review_callback."""
    update = MagicMock()
    update.effective_chat = MagicMock()
    update.effective_chat.id = chat_id

    query = MagicMock()
    query.data = "start_review"
    query.answer = AsyncMock()

    if message_is_accessible:
        # spec=telegram.Message makes isinstance(query.message, Message) == True
        fake_msg = MagicMock(spec=telegram.Message)
        fake_msg.reply_text = AsyncMock()
    else:
        # A plain object (not spec'd to telegram.Message) simulates InaccessibleMessage —
        # isinstance(fake_msg, telegram.Message) returns False for it.
        class _FakeInaccessibleMessage:
            pass

        fake_msg = _FakeInaccessibleMessage()

    query.message = fake_msg
    update.callback_query = query

    context = MagicMock()
    config = _make_config()

    context.application = MagicMock()
    context.application.bot_data = {
        "config": config,
        "db_pool": AsyncMock(),
        "http_client": AsyncMock(),
    }

    return update, context


# ---------------------------------------------------------------------------
# TG-002: rate_limit on show_answer (max 10/min)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_show_answer_rate_limited_after_10_calls():
    """TG-002: show_answer is rate-limited at 10 calls per 60 seconds.

    The 11th call within the window should be rejected by the rate_limit
    decorator and return None instead of ConversationHandler state.
    """
    _timestamps.clear()

    from telegram_bot.handlers.review_handler import show_answer

    sample_card = {
        "id": 1,
        "deck_id": 1,
        "paper_id": None,
        "card_type": "concept",
        "front": "Q?",
        "back": "A.",
        "evidence": {},
        "fsrs_state": {},
        "due_at": "2026-03-01T00:00:00Z",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }

    # Make 10 successful calls
    for _ in range(10):
        update, context, _ = _make_callback_update_and_context("show_answer")
        context.user_data = {"current_card": sample_card, "cards_reviewed": 0}
        result = await show_answer(update, context)
        # Each should succeed (SHOWING_BACK = 1)
        assert result is not None, "First 10 calls should succeed"

    # 11th call from same chat should be rate-limited
    update, context, _ = _make_callback_update_and_context("show_answer")
    context.user_data = {"current_card": sample_card, "cards_reviewed": 0}
    result = await show_answer(update, context)

    assert result is None, (
        "11th call to show_answer within 60s should be rate-limited (returns None)"
    )


# ---------------------------------------------------------------------------
# TG-002: rate_limit on rate_card (max 5/min)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_card_rate_limited_after_5_calls():
    """TG-002: rate_card is rate-limited at 5 calls per 60 seconds.

    The 6th call within the window should be rejected by the rate_limit
    decorator and return None instead of ConversationHandler state.
    """
    _timestamps.clear()

    from telegram_bot.handlers.review_handler import rate_card

    sample_card = {
        "id": 1,
        "deck_id": 1,
        "paper_id": None,
        "card_type": "concept",
        "front": "Q?",
        "back": "A.",
        "evidence": {},
        "fsrs_state": {},
        "due_at": "2026-03-01T00:00:00Z",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }

    submit_resp = MagicMock()
    submit_resp.raise_for_status = MagicMock()
    submit_resp.json.return_value = {"next_due_at": "2026-04-01T00:00:00Z"}

    next_resp = MagicMock()
    next_resp.raise_for_status = MagicMock()
    next_resp.json.return_value = []  # no more cards — each call ends the session

    # Make 5 successful calls
    for _ in range(5):
        update, context, mock_http = _make_callback_update_and_context("rate_3")
        context.user_data = {"current_card": sample_card, "cards_reviewed": 0}
        mock_http.post.return_value = submit_resp
        mock_http.get.return_value = next_resp
        result = await rate_card(update, context)
        assert result is not None, "First 5 calls should succeed"

    # 6th call from same chat should be rate-limited
    update, context, mock_http = _make_callback_update_and_context("rate_3")
    context.user_data = {"current_card": sample_card, "cards_reviewed": 0}
    mock_http.post.return_value = submit_resp
    mock_http.get.return_value = next_resp
    result = await rate_card(update, context)

    assert result is None, "6th call to rate_card within 60s should be rate-limited (returns None)"


# ---------------------------------------------------------------------------
# TG-003: start_review_callback handles inaccessible message
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_review_callback_handles_inaccessible_message_gracefully():
    """TG-003: start_review_callback sends alert and returns early when the
    message is inaccessible (not an instance of telegram.Message).

    Previously, the bare ``update.message = query.message`` assignment silently
    accepted an InaccessibleMessage object.  The fix guards this with an
    isinstance check and answers with a show_alert if the message is gone.
    """
    from telegram_bot.handlers.callback_handler import start_review_callback

    # Build an update where query.message is NOT a telegram.Message instance
    update, context = _make_start_review_update_and_context(message_is_accessible=False)
    query = update.callback_query

    with patch(
        "telegram_bot.handlers.callback_handler.review_start", new_callable=AsyncMock
    ) as mock_review_start:
        await start_review_callback(update, context)

    # review_start must NOT have been called
    mock_review_start.assert_not_awaited()

    # query.answer must have been called with show_alert=True
    query.answer.assert_awaited()
    answer_kwargs = query.answer.await_args
    assert answer_kwargs is not None
    # show_alert=True should be in kwargs
    kwargs = answer_kwargs[1] if answer_kwargs[1] else {}
    assert kwargs.get("show_alert") is True, (
        "query.answer must be called with show_alert=True for inaccessible message"
    )
