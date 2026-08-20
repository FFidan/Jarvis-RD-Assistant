"""Flashcard review conversation handler for the JARVIS Telegram bot.

Implements a multi-step review flow using ``ConversationHandler``:
SHOWING_FRONT -> SHOWING_BACK -> (loop or END).
"""

from __future__ import annotations

import logging
import re
from html import escape as _html_escape

from jarvis_common.time_utils import utc_now_iso
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
)

from telegram_bot import services_client
from telegram_bot.command_catalog import command_spec
from telegram_bot.formatters import format_card_back, format_card_front
from telegram_bot.handlers.helpers import (
    auth_check,
    get_config,
    get_http,
    get_jarvis_user_id,
    get_platform_http,
)
from telegram_bot.handlers.rate_limit import rate_limit

logger = logging.getLogger(__name__)

_REVIEW_COMMAND = command_spec("review").name
_CANCEL_COMMAND = command_spec("cancel").name


class _CardFetchError(Exception):
    """Raised when the learning engine request fails (distinct from an empty queue)."""


# Conversation states
SHOWING_FRONT = 0
SHOWING_BACK = 1

# Rating labels
RATING_LABELS = {1: "Again", 2: "Hard", 3: "Good", 4: "Easy"}

# Guard against malformed callback data before int() parse
_RATING_RE = re.compile(r"^rate_([1-4])$")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _fetch_next_card(context: ContextTypes.DEFAULT_TYPE) -> dict | None:
    """Fetch the next due card from the learning engine.

    Returns the next card dict, or ``None`` when the queue is empty.
    Raises ``_CardFetchError`` on network / API errors so callers can
    distinguish a real failure from a genuinely empty queue.
    """
    http = get_http(context)
    config = get_config(context)
    jarvis_user_id = get_jarvis_user_id(context)
    assert jarvis_user_id is not None  # noqa: S101 — guaranteed by auth_check invariant
    try:
        cards = await services_client.fetch_next_review_card(http, config, jarvis_user_id)
        if isinstance(cards, list) and cards:
            return cards[0]
        return None
    except Exception as exc:
        logger.exception("Failed to fetch next review card")
        raise _CardFetchError() from exc


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


@rate_limit(max_calls=5, window_seconds=60)
async def review_start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    message: Message | None = None,
) -> int:
    """Handle ``/review`` — begin a flashcard review session.

    Args:
        update: The incoming Update from python-telegram-bot.
        context: The handler context.
        message: Explicit ``Message`` to reply to.  When omitted, falls back to
            ``update.message`` (command path) and then to
            ``update.callback_query.message`` (callback path).  Passing this
            explicitly avoids mutating ``update.message`` from callback sites.
    """
    # H6: on the inline-button entry path, answer the callback query up front —
    # before ANY early return — or the user's Telegram client shows a loading
    # spinner indefinitely (unauthorized / no-due-cards / missing-message paths
    # all end the conversation without otherwise acknowledging the button).
    if update.callback_query is not None:
        await update.callback_query.answer()

    # Resolve the reply target without mutating the Update object.
    msg: Message | None = message
    if msg is None:
        msg = update.message
    if msg is None and update.callback_query is not None:
        raw = update.callback_query.message
        msg = raw if isinstance(raw, Message) else None

    if msg is None or context.user_data is None:
        return ConversationHandler.END

    config = get_config(context)
    platform_client = get_platform_http(context)
    authorized, jarvis_user_id = await auth_check(update, config, platform_client)
    if not authorized:
        return ConversationHandler.END

    # Refresh the cached user identity so subsequent intra-session calls
    # (e.g. _fetch_next_card, rate_card) use the current pairing, not a
    # potentially stale value from a previous pair.
    context.user_data["jarvis_user_id"] = jarvis_user_id

    context.user_data["cards_reviewed"] = 0
    context.user_data["review_start_time"] = utc_now_iso()

    try:
        card = await _fetch_next_card(context)
    except _CardFetchError:
        await msg.reply_text("Couldn't load cards — try /review again.", parse_mode="HTML")
        return ConversationHandler.END
    if card is None:
        await msg.reply_text("No cards due! You're all caught up.", parse_mode="HTML")
        return ConversationHandler.END

    context.user_data["current_card"] = card
    text = format_card_front(card)
    await msg.reply_text(text, parse_mode="HTML", reply_markup=_show_answer_keyboard())
    return SHOWING_FRONT


# ---------------------------------------------------------------------------
# Show answer
# ---------------------------------------------------------------------------


@rate_limit(max_calls=10, window_seconds=60)
async def show_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle 'Show Answer' button — edit the message to reveal the card back."""
    query = update.callback_query
    if query is None or context.user_data is None:
        return ConversationHandler.END

    config = get_config(context)
    platform_client = get_platform_http(context)
    authorized, jarvis_user_id = await auth_check(update, config, platform_client)
    if not authorized:
        await query.answer()
        return ConversationHandler.END

    # Keep cached identity current in case pairing changed mid-session.
    context.user_data["jarvis_user_id"] = jarvis_user_id

    await query.answer()

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


@rate_limit(max_calls=5, window_seconds=60)
async def rate_card(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle rating callback — POST rating to learning engine then fetch and show next card."""
    query = update.callback_query
    if query is None or query.data is None or context.user_data is None:
        return ConversationHandler.END

    config = get_config(context)
    platform_client = get_platform_http(context)
    authorized, jarvis_user_id = await auth_check(update, config, platform_client)
    if not authorized:
        await query.answer()
        return ConversationHandler.END
    assert jarvis_user_id is not None  # noqa: S101 — guaranteed by auth_check invariant

    # Refresh cached identity — use the freshly resolved id for this request
    # rather than whatever was stored from a potentially stale prior session.
    context.user_data["jarvis_user_id"] = jarvis_user_id

    _m = _RATING_RE.match(query.data or "")
    if not _m:
        logger.warning("rate_card: unexpected query.data=%r", query.data)
        await query.answer(text="Invalid input. Use /review to restart.")
        return ConversationHandler.END

    await query.answer()

    rating = int(_m.group(1))
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
        result = await services_client.submit_review_rating(
            http, config, jarvis_user_id, card_id, rating
        )
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
    try:
        next_card = await _fetch_next_card(context)
    except _CardFetchError:
        await query.edit_message_text(
            f"Rated: <b>{label}</b>. Next review: {next_review_str}\n\n"
            "Couldn't load cards — try /review again.",
            parse_mode="HTML",
        )
        return ConversationHandler.END
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
    platform_client = get_platform_http(context)
    authorized, _ = await auth_check(update, config, platform_client)
    if not authorized:
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
            CommandHandler(_REVIEW_COMMAND, review_start),
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
        fallbacks=[CommandHandler(_CANCEL_COMMAND, cancel_review)],
        # A session the user walked away from keeps its state indefinitely, and
        # the entry points are only searched when the conversation has no state
        # or re-entry is allowed. Without this, a later /review is claimed by
        # the stale session and answered by nothing at all.
        allow_reentry=True,
    )
