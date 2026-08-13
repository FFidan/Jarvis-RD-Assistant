"""Tests for Telegram bot flashcard review conversation handler.

Covers: review_start, show_answer, rate_card, cancel_review.
Each handler is tested directly with mocked Update + Context objects.

Regex-guard tests are at the bottom of this module.
Verified: handlers/review_handler.py:204 (_RATING_RE guard in rate_card)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from jarvis_common.testing import PTBContextOptions, make_bot_config, make_ptb_context
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


def _make_command_update_and_context(chat_id=_TEST_CHAT_ID):
    """Build mock Update + Context for /review command entry."""
    update = MagicMock()
    update.effective_chat = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_chat.type = "private"
    update.message = MagicMock()
    update.message.reply_text = AsyncMock()
    # A real command Update carries no callback_query — keep the mock faithful
    # (review_start branches on `update.callback_query is not None`).
    update.callback_query = None

    config = make_bot_config(BotConfig)
    mock_http = AsyncMock()
    context = make_ptb_context(
        AsyncMock(), config, options=PTBContextOptions(http_client=mock_http)
    )

    return update, context, mock_http


def _make_callback_update_and_context(callback_data: str, user_data=None, chat_id=_TEST_CHAT_ID):
    """Build mock Update + Context for callback queries during review."""
    update = MagicMock()
    update.effective_chat = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_chat.type = "private"

    query = MagicMock()
    query.data = callback_data
    query.answer = AsyncMock()
    query.message = MagicMock()
    query.message.reply_text = AsyncMock()
    query.edit_message_text = AsyncMock()
    update.callback_query = query

    config = make_bot_config(BotConfig)
    mock_http = AsyncMock()
    context = make_ptb_context(
        AsyncMock(),
        config,
        options=PTBContextOptions(http_client=mock_http, user_data=user_data),
    )

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
async def test_review_start_fetch_error_shows_error_message():
    """/review with a fetch error shows an error message, not 'all caught up'."""
    update, context, mock_http = _make_command_update_and_context()
    mock_http.get.side_effect = Exception("connection refused")

    result = await review_start(update, context)

    assert result == ConversationHandler_END
    text = update.message.reply_text.call_args[0][0]
    assert "caught up" not in text.lower()
    assert "/review" in text


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
async def test_rate_card_next_fetch_error_shows_error_message():
    """A fetch error after rating shows an error message, not 'session complete'."""
    card = _sample_card()
    user_data = {"current_card": card, "cards_reviewed": 1}
    update, context, mock_http = _make_callback_update_and_context("rate_3", user_data=user_data)

    submit_resp = MagicMock()
    submit_resp.raise_for_status = MagicMock()
    submit_resp.json.return_value = {"next_due_at": "2026-03-10T00:00:00Z"}

    mock_http.post.return_value = submit_resp
    mock_http.get.side_effect = Exception("network error")

    result = await rate_card(update, context)

    assert result == ConversationHandler_END
    text = update.callback_query.edit_message_text.call_args[0][0]
    assert "complete" not in text.lower()
    assert "/review" in text


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
    update, context, _mock_http = _make_callback_update_and_context("rate_3", user_data=user_data)

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


# ---------------------------------------------------------------------------
# Tests: regex guard on query.data
# ---------------------------------------------------------------------------

# Use a distinct chat_id so the rate-limiter bucket for these tests is
# isolated from the 5-call quota consumed by the test_rate_card_* tests above.
# NOTE: must NOT be 99999 — that is _TEST_CHAT_ID in test_review_handler_reauth.py,
# and the shared module-level _timestamps dict in rate_limit.py is not reset between
# test files, so reusing 99999 here fills the 5-call quota before the reauth tests run.
_SEC_CHAT_ID = 55555


@pytest.mark.asyncio
async def test_rate_card_rejects_malformed_query_data() -> None:
    """Non-integer / out-of-range rating in query.data must not raise ValueError.

    Bare int(query.data.split('_')[1]) replaced with _RATING_RE guard.
    """
    user_data = {"current_card": _sample_card(), "cards_reviewed": 0}
    update, context, _mock_http = _make_callback_update_and_context(
        "rate_not_a_number", user_data=user_data, chat_id=_SEC_CHAT_ID
    )

    # Must not raise; should answer and return END
    result = await rate_card(update, context)

    assert result == ConversationHandler_END
    update.callback_query.answer.assert_awaited()


@pytest.mark.asyncio
async def test_rate_card_rejects_out_of_range_rating() -> None:
    """Rating values outside 1-4 (e.g. rate_9) are rejected by the regex guard."""
    user_data = {"current_card": _sample_card(), "cards_reviewed": 0}
    update, context, _mock_http = _make_callback_update_and_context(
        "rate_9", user_data=user_data, chat_id=_SEC_CHAT_ID
    )

    result = await rate_card(update, context)

    assert result == ConversationHandler_END
    update.callback_query.answer.assert_awaited()


@pytest.mark.asyncio
async def test_rate_card_rejects_injected_prefix() -> None:
    """Prefixed payloads like 'evil_rate_3' are rejected by the regex guard."""
    user_data = {"current_card": _sample_card(), "cards_reviewed": 0}
    update, context, _mock_http = _make_callback_update_and_context(
        "evil_rate_3", user_data=user_data, chat_id=_SEC_CHAT_ID
    )

    result = await rate_card(update, context)

    assert result == ConversationHandler_END
    update.callback_query.answer.assert_awaited()


# Distinct chat_id bucket so this test doesn't tip the 5-call rate-limiter
# quota shared by the three regex-guard tests above (which would
# bleed quota exhaustion into test_review_handler_reauth.py order-dependent).
_W2_CF4_CHAT_ID = 77777


@pytest.mark.asyncio
async def test_rate_card_malformed_data_answers_with_text() -> None:
    """Malformed query.data must answer with user-facing text.

    Bare query.answer() leaves Telegram UI showing live (now non-functional) buttons.
    The fix passes text= so Telegram dismisses the spinner with a visible message.
    """
    user_data = {"current_card": _sample_card(), "cards_reviewed": 0}
    update, context, _mock_http = _make_callback_update_and_context(
        "garbage", user_data=user_data, chat_id=_W2_CF4_CHAT_ID
    )

    result = await rate_card(update, context)

    assert result == ConversationHandler_END
    update.callback_query.answer.assert_awaited_once_with(
        text="Invalid input. Use /review to restart."
    )


@pytest.mark.asyncio
async def test_rate_card_valid_rating_parses_correctly() -> None:
    """Valid query.data='rate_3' parses rating as integer 3 (happy path)."""
    card = _sample_card()
    next_card = {
        **_sample_card(),
        "id": 2,
        "front": "What is NLP?",
        "back": "Natural Language Processing",
    }
    user_data = {"current_card": card, "cards_reviewed": 0}
    update, context, mock_http = _make_callback_update_and_context(
        "rate_3", user_data=user_data, chat_id=_SEC_CHAT_ID
    )

    submit_resp = MagicMock()
    submit_resp.raise_for_status = MagicMock()
    submit_resp.json.return_value = {"next_due_at": "2026-03-10T00:00:00Z"}

    next_resp = MagicMock()
    next_resp.raise_for_status = MagicMock()
    next_resp.json.return_value = [next_card]

    mock_http.post.return_value = submit_resp
    mock_http.get.return_value = next_resp

    result = await rate_card(update, context)

    # Rating 3 = "Good" — verify payload sent to API contains rating=3
    assert result == SHOWING_FRONT
    post_call_kwargs = mock_http.post.call_args[1]
    assert post_call_kwargs["json"]["rating"] == 3


# ---------------------------------------------------------------------------
# Tests: behavioral coverage for ratings 1 and 2
# ---------------------------------------------------------------------------

# Separate chat_id bucket so the 5-call rate-limiter quota from _TEST_CHAT_ID
# and _SEC_CHAT_ID tests does not bleed into these two tests.
_RATING_12_CHAT_ID = 88888


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rating, expected_label",
    [
        (1, "Again"),
        (2, "Hard"),
    ],
)
async def test_rate_card_ratings_1_and_2_send_correct_payload(
    rating: int, expected_label: str
) -> None:
    """Ratings 1 ('Again') and 2 ('Hard') POST the correct integer to the API.

    Behavioral coverage for ratings 1 and 2, mirroring the rating-3
    test in test_rate_card_valid_rating_parses_correctly.
    """
    card = _sample_card()
    next_card = {
        **_sample_card(),
        "id": 2,
        "front": "What is NLP?",
        "back": "Natural Language Processing",
    }
    user_data = {"current_card": card, "cards_reviewed": 0}
    update, context, mock_http = _make_callback_update_and_context(
        f"rate_{rating}", user_data=user_data, chat_id=_RATING_12_CHAT_ID
    )

    submit_resp = MagicMock()
    submit_resp.raise_for_status = MagicMock()
    submit_resp.json.return_value = {"next_due_at": "2026-06-01T00:00:00Z"}

    next_resp = MagicMock()
    next_resp.raise_for_status = MagicMock()
    next_resp.json.return_value = [next_card]

    mock_http.post.return_value = submit_resp
    mock_http.get.return_value = next_resp

    result = await rate_card(update, context)

    # Handler must advance to next card
    assert result == SHOWING_FRONT
    assert context.user_data["current_card"] == next_card
    assert context.user_data["cards_reviewed"] == 1

    # API POST body must carry the exact integer rating (not a string or wrong value)
    post_call_kwargs = mock_http.post.call_args[1]
    assert post_call_kwargs["json"]["rating"] == rating

    # Response text must contain the human-readable label for the rating
    edit_text = update.callback_query.edit_message_text.call_args[0][0]
    assert expected_label in edit_text


# ---------------------------------------------------------------------------
# Tests: review_start via the inline "Start Review" button must answer
# the callback query (otherwise the Telegram client spins forever)
# ---------------------------------------------------------------------------

# Separate chat_id bucket so review_start's 5-call rate-limit quota from the
# command-path tests above does not bleed into these callback-path tests.
_H6_CHAT_ID = 22222


def _make_start_review_callback_update(chat_id=_H6_CHAT_ID):
    """Build a faithful callback-entry Update for review_start (start_review button)."""
    from telegram import Message

    update, context, mock_http = _make_callback_update_and_context("start_review", chat_id=chat_id)
    # A callback Update carries no update.message; the reply target is the
    # callback query's message (spec'd so isinstance(raw, Message) passes).
    update.message = None
    update.callback_query.message = MagicMock(spec=Message)
    return update, context, mock_http


@pytest.mark.asyncio
async def test_review_start_callback_answers_query_when_no_cards_due() -> None:
    """Inline-button entry must answer the callback query even on the
    no-due-cards early END — otherwise the button spinner never stops (H6)."""
    update, context, mock_http = _make_start_review_callback_update()
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = []
    mock_http.get.return_value = mock_resp

    result = await review_start(update, context)

    assert result == ConversationHandler_END
    update.callback_query.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_review_start_callback_answers_query_on_auth_fail() -> None:
    """Inline-button entry must answer the callback query even when auth_check
    denies and the handler ENDs without any user-visible reply (H6)."""
    from unittest.mock import patch

    update, context, _mock_http = _make_start_review_callback_update()

    with patch(
        "telegram_bot.handlers.review_handler.auth_check",
        new=AsyncMock(return_value=(False, None)),
    ):
        result = await review_start(update, context)

    assert result == ConversationHandler_END
    update.callback_query.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_review_start_command_path_does_not_touch_callback_query() -> None:
    """/review (command entry) has no callback query — the ack must be a
    no-op there and the first card still renders."""
    update, context, mock_http = _make_command_update_and_context()
    card = _sample_card()
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = [card]
    mock_http.get.return_value = mock_resp

    result = await review_start(update, context)

    assert result == SHOWING_FRONT
    assert update.callback_query is None  # nothing answered, nothing mutated


# ---------------------------------------------------------------------------
# Tests: query.answer() on auth-fail branches
# ---------------------------------------------------------------------------

# Separate chat_id bucket to avoid bleed from other rate-limiter quotas.
_AUTH_FAIL_CHAT_ID = 11111


@pytest.mark.asyncio
async def test_show_answer_auth_fail_calls_query_answer() -> None:
    """show_answer must call query.answer() before returning END on auth failure.

    Without this call Telegram leaves a permanent loading spinner on the inline
    button even though the bot rejected the action.
    """
    from unittest.mock import patch

    from telegram.ext import ConversationHandler

    update, context, _mock_http = _make_callback_update_and_context(
        "show_answer", chat_id=_AUTH_FAIL_CHAT_ID
    )

    with patch(
        "telegram_bot.handlers.review_handler.auth_check",
        new=AsyncMock(return_value=(False, None)),
    ):
        result = await show_answer(update, context)

    assert result == ConversationHandler.END
    update.callback_query.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_rate_card_auth_fail_calls_query_answer() -> None:
    """rate_card must call query.answer() before returning END on auth failure.

    Without this call Telegram leaves a permanent loading spinner on the inline
    button even though the bot rejected the action.
    """
    from unittest.mock import patch

    from telegram.ext import ConversationHandler

    update, context, _mock_http = _make_callback_update_and_context(
        "rate_3", chat_id=_AUTH_FAIL_CHAT_ID
    )

    with patch(
        "telegram_bot.handlers.review_handler.auth_check",
        new=AsyncMock(return_value=(False, None)),
    ):
        result = await rate_card(update, context)

    assert result == ConversationHandler.END
    update.callback_query.answer.assert_awaited_once()
