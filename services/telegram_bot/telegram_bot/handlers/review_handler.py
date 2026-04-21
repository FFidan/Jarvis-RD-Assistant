"""Flashcard review conversation handler for the JARVIS Telegram bot.

Implements a multi-step review flow using ``ConversationHandler``:
SHOWING_FRONT -> SHOWING_BACK -> (loop or END).
"""

from __future__ import annotations

import logging
from html import escape as _html_escape

from jarvis_common.time_utils import utc_now_iso
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
)

from telegram_bot.formatters import format_card_back, format_card_front
from telegram_bot.handlers.helpers import auth_check, get_config, get_db, get_http

logger = logging.getLogger(__name__)

# Conversation states
SHOWING_FRONT = 0
SHOWING_BACK = 1

# Rating labels
RATING_LABELS = {1: "Again", 2: "Hard", 3: "Good", 4: "Easy"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _fetch_next_card(context: ContextTypes.DEFAULT_TYPE) -> dict | None:
    """Fetch the next due card from the learning engine; returns None when none are due."""
    http = get_http(context)
    config = get_config(context)
    try:
        resp = await http.get(
            f"{config.learning_engine_url}/api/review/next",
            params={"limit": 1},
            timeout=15.0,
        )
        resp.raise_for_status()
        cards = resp.json()
        if isinstance(cards, list) and cards:
            return cards[0]
        return None
    except Exception:
        logger.exception("Failed to fetch next review card")
        return None


def _show_answer_keyboard() -> InlineKeyboardMarkup:
    """Keyboard with a single 'Show Answer' button."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Show Answer", callback_data="show_answer")]]
    )


def _rating_keyboard() -> InlineKeyboardMarkup:
    """Keyboard with rating buttons."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Again (1)", callback_data="rate_1"),
                InlineKeyboardButton("Hard (2)", callback_data="rate_2"),
                InlineKeyboardButton("Good (3)", callback_data="rate_3"),
                InlineKeyboardButton("Easy (4)", callback_data="rate_4"),
            ]
        ]
    )


# ---------------------------------------------------------------------------
# Conversation entry
# ---------------------------------------------------------------------------


async def review_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle ``/review`` — begin a flashcard review session."""
    if update.message is None or context.user_data is None:
        return ConversationHandler.END

    config = get_config(context)
    db_pool = get_db(context)
    if not await auth_check(update, config, db_pool):
        return ConversationHandler.END

    context.user_data["cards_reviewed"] = 0
    context.user_data["review_start_time"] = utc_now_iso()

    card = await _fetch_next_card(context)
    if card is None:
        await update.message.reply_text("No cards due! You're all caught up.", parse_mode="HTML")
        return ConversationHandler.END

    context.user_data["current_card"] = card
    text = format_card_front(card)
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=_show_answer_keyboard())
    return SHOWING_FRONT


# ---------------------------------------------------------------------------
# Show answer
# ---------------------------------------------------------------------------


async def show_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle 'Show Answer' button — edit the message to reveal the card back."""
    query = update.callback_query
    if query is None or context.user_data is None:
        return ConversationHandler.END
    await query.answer()

    config = get_config(context)
    db_pool = get_db(context)
    if not await auth_check(update, config, db_pool):
        return ConversationHandler.END

    card = context.user_data.get("current_card")
    if not card:
        await query.edit_message_text(
            "Session expired. Use /review to start again.", parse_mode="HTML"
        )
        return ConversationHandler.END

    text = format_card_back(card)
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=_rating_keyboard())
    return SHOWING_BACK


# ---------------------------------------------------------------------------
# Rate card
# ---------------------------------------------------------------------------


async def rate_card(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle rating callback — POST rating to learning engine then fetch and show next card."""
    query = update.callback_query
    if query is None or query.data is None or context.user_data is None:
        return ConversationHandler.END
    await query.answer()

    config = get_config(context)
    db_pool = get_db(context)
    if not await auth_check(update, config, db_pool):
        return ConversationHandler.END

    rating = int(query.data.split("_")[1])
    label = RATING_LABELS.get(rating, str(rating))
    card = context.user_data.get("current_card")

    if not card:
        await query.edit_message_text(
            "Session expired. Use /review to start again.", parse_mode="HTML"
        )
        return ConversationHandler.END

    card_id = card.get("id")
    if not card_id:
        await query.edit_message_text("Invalid card data. Please try /review again.")
        return ConversationHandler.END

    # Submit rating to learning engine
    http = get_http(context)
    next_review_str = "unknown"
    review_ok = True
    try:
        resp = await http.post(
            f"{config.learning_engine_url}/api/review/{card_id}",
            json={"rating": rating},
            timeout=15.0,
        )
        resp.raise_for_status()
        result = resp.json()
        next_review_str = _html_escape(result.get("next_due_at", "unknown"))
    except Exception:
        logger.exception("Failed to submit review for card %s", card_id)
        review_ok = False

    if not review_ok:
        await query.edit_message_text(
            "Failed to save review. Please try /review again.",
            parse_mode="HTML",
        )
        return ConversationHandler.END

    context.user_data["cards_reviewed"] = context.user_data.get("cards_reviewed", 0) + 1

    # Fetch next card
    next_card = await _fetch_next_card(context)
    if next_card is None:
        reviewed = context.user_data.get("cards_reviewed", 0)
        await query.edit_message_text(
            f"Rated: <b>{label}</b>. Next review: {next_review_str}\n\n"
            f"🎉 Review session complete! Cards reviewed: <b>{reviewed}</b>",
            parse_mode="HTML",
        )
        return ConversationHandler.END

    context.user_data["current_card"] = next_card
    text = (
        f"Rated: <b>{label}</b>. Next review: {next_review_str}\n\n"
        "━━━━━━━━━━━━━━━━━━\n\n" + format_card_front(next_card)
    )
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=_show_answer_keyboard())
    return SHOWING_FRONT


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------


async def cancel_review(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle ``/cancel`` — end the review session early and report cards reviewed."""
    if update.message is None or context.user_data is None:
        return ConversationHandler.END

    config = get_config(context)
    db_pool = get_db(context)
    if not await auth_check(update, config, db_pool):
        return ConversationHandler.END

    reviewed = context.user_data.get("cards_reviewed", 0)
    await update.message.reply_text(
        f"Review session ended. Cards reviewed: <b>{reviewed}</b>",
        parse_mode="HTML",
    )
    context.user_data.pop("current_card", None)
    context.user_data.pop("cards_reviewed", None)
    context.user_data.pop("review_start_time", None)
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------


def get_review_conversation_handler() -> ConversationHandler:
    """Build and return the review ``ConversationHandler`` for flashcard sessions."""
    return ConversationHandler(
        entry_points=[
            CommandHandler("review", review_start),
            # Allow the review flow to be triggered by the inline "Start review"
            # button (callback_data="start_review") in addition to the /review command.
            CallbackQueryHandler(review_start, pattern=r"^start_review$"),
        ],
        states={
            SHOWING_FRONT: [
                CallbackQueryHandler(show_answer, pattern=r"^show_answer$"),
            ],
            SHOWING_BACK: [
                CallbackQueryHandler(rate_card, pattern=r"^rate_[1-4]$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_review)],
    )
