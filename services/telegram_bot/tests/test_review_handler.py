"""Tests for Telegram bot flashcard review conversation handler.

Covers: review_start, show_answer, rate_card, cancel_review.
Each handler is tested directly with mocked Update + Context objects.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram_bot.config import BotConfig
from telegram_bot.handlers.review_handler import (  # noqa: E402
    SHOWING_BACK,
    SHOWING_FRONT,
    cancel_review,
    rate_card,
    review_start,
    show_answer,
)

# Use the real END value from the stub
ConversationHandler_END = -1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


def _make_command_update_and_context(chat_id=_TEST_CHAT_ID):
    """Build mock Update + Context for /review command entry."""
    update = MagicMock()
    update.effective_chat = MagicMock()
    update.effective_chat.id = chat_id
    update.message = MagicMock()
    update.message.reply_text = AsyncMock()

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


def _make_callback_update_and_context(callback_data: str, user_data=None, chat_id=_TEST_CHAT_ID):
    """Build mock Update + Context for callback queries during review."""
    update = MagicMock()
    update.effective_chat = MagicMock()
    update.effective_chat.id = chat_id

    query = MagicMock()
    query.data = callback_data
    query.answer = AsyncMock()
    query.message = MagicMock()
    query.message.reply_text = AsyncMock()
    query.edit_message_text = AsyncMock()
    update.callback_query = query

    context = MagicMock()
    context.user_data = user_data if user_data is not None else {}
    config = _make_config()
    mock_http = AsyncMock()

    context.application = MagicMock()
    context.application.bot_data = {
        "config": config,
        "db_pool": AsyncMock(),
        "http_client": mock_http,
    }

    return update, context, mock_http


def _sample_card():
    return {
        "id": 1,
        "deck_id": 1,
        "paper_id": None,
        "card_type": "concept",
        "front": "What is ML?",
        "back": "Machine Learning",
        "evidence": {},
        "fsrs_state": {},
        "due_at": "2026-03-01T00:00:00Z",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }


# ---------------------------------------------------------------------------
# Tests: review_start (/review command)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_review_start_no_cards():
    """/review with no due cards ends the conversation."""
    update, context, mock_http = _make_command_update_and_context()
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = []
    mock_http.get.return_value = mock_resp

    result = await review_start(update, context)

    assert result == ConversationHandler_END
    text = update.message.reply_text.call_args[0][0]
    assert "caught up" in text.lower() or "No cards" in text


@pytest.mark.asyncio
async def test_review_start_shows_first_card():
    """/review with a due card shows the card front."""
    update, context, mock_http = _make_command_update_and_context()
    card = _sample_card()
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = [card]
    mock_http.get.return_value = mock_resp

    result = await review_start(update, context)

    assert result == SHOWING_FRONT
    assert context.user_data["current_card"] == card
    update.message.reply_text.assert_awaited_once()


# ---------------------------------------------------------------------------
# Tests: show_answer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_show_answer_success():
    """Show answer reveals card back and rating keyboard."""
    card = _sample_card()
    user_data = {"current_card": card, "cards_reviewed": 0}
    update, context, _ = _make_callback_update_and_context("show_answer", user_data=user_data)

    result = await show_answer(update, context)

    assert result == SHOWING_BACK
    update.callback_query.answer.assert_awaited_once()
    update.callback_query.edit_message_text.assert_awaited_once()
    text = update.callback_query.edit_message_text.call_args[0][0]
    assert "Machine Learning" in text


@pytest.mark.asyncio
async def test_show_answer_expired_session():
    """Show answer with no current_card ends the conversation."""
    user_data = {}  # no current_card
    update, context, _ = _make_callback_update_and_context("show_answer", user_data=user_data)

    result = await show_answer(update, context)

    assert result == ConversationHandler_END
    update.callback_query.edit_message_text.assert_awaited_once()
    text = update.callback_query.edit_message_text.call_args[0][0]
    assert "expired" in text.lower() or "/review" in text


# ---------------------------------------------------------------------------
# Tests: rate_card
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_card_next_card():
    """Rating a card with more due cards shows the next card."""
    card = _sample_card()
    next_card = {**_sample_card(), "id": 2, "front": "What is DL?", "back": "Deep Learning"}
    user_data = {"current_card": card, "cards_reviewed": 0}
    update, context, mock_http = _make_callback_update_and_context("rate_3", user_data=user_data)

    # Submit review response
    submit_resp = MagicMock()
    submit_resp.raise_for_status = MagicMock()
    submit_resp.json.return_value = {"next_due_at": "2026-03-10T00:00:00Z"}

    # Next card fetch response
    next_resp = MagicMock()
    next_resp.raise_for_status = MagicMock()
    next_resp.json.return_value = [next_card]

    mock_http.post.return_value = submit_resp
    mock_http.get.return_value = next_resp

    result = await rate_card(update, context)

    assert result == SHOWING_FRONT
    assert context.user_data["current_card"] == next_card
    assert context.user_data["cards_reviewed"] == 1


@pytest.mark.asyncio
async def test_rate_card_last_card():
    """Rating the last card ends the session with completion message."""
    card = _sample_card()
    user_data = {"current_card": card, "cards_reviewed": 2}
    update, context, mock_http = _make_callback_update_and_context("rate_4", user_data=user_data)

    submit_resp = MagicMock()
    submit_resp.raise_for_status = MagicMock()
    submit_resp.json.return_value = {"next_due_at": "2026-03-10T00:00:00Z"}

    # No more cards
    next_resp = MagicMock()
    next_resp.raise_for_status = MagicMock()
    next_resp.json.return_value = []

    mock_http.post.return_value = submit_resp
    mock_http.get.return_value = next_resp

    result = await rate_card(update, context)

    assert result == ConversationHandler_END
    text = update.callback_query.edit_message_text.call_args[0][0]
    assert "complete" in text.lower() or "3" in text  # 2 + 1 = 3 reviewed


@pytest.mark.asyncio
async def test_rate_card_api_failure():
    """Rating a card with API failure ends the session with error."""
    card = _sample_card()
    user_data = {"current_card": card, "cards_reviewed": 0}
    update, context, mock_http = _make_callback_update_and_context("rate_3", user_data=user_data)
    mock_http.post.side_effect = Exception("network error")

    result = await rate_card(update, context)

    assert result == ConversationHandler_END
    text = update.callback_query.edit_message_text.call_args[0][0]
    assert "Failed" in text or "failed" in text.lower()


@pytest.mark.asyncio
async def test_rate_card_expired_session():
    """Rating with no current card ends the session."""
    user_data = {}  # no current_card
    update, context, mock_http = _make_callback_update_and_context("rate_3", user_data=user_data)

    result = await rate_card(update, context)

    assert result == ConversationHandler_END
    text = update.callback_query.edit_message_text.call_args[0][0]
    assert "expired" in text.lower() or "/review" in text


# ---------------------------------------------------------------------------
# Tests: cancel_review
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_review():
    """/cancel ends the review session and reports cards reviewed."""
    update, context, _ = _make_command_update_and_context()
    context.user_data["cards_reviewed"] = 5
    context.user_data["current_card"] = _sample_card()

    result = await cancel_review(update, context)

    assert result == ConversationHandler_END
    text = update.message.reply_text.call_args[0][0]
    assert "5" in text
    assert "current_card" not in context.user_data
