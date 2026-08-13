"""System command handlers: /start, /help, /focus."""

from __future__ import annotations

import logging

import httpx
from telegram import Update
from telegram.ext import ContextTypes

from telegram_bot import services_client
from telegram_bot.formatters import format_help
from telegram_bot.handlers.commands._auth import auth_required
from telegram_bot.handlers.helpers import (
    auth_check,
    get_config,
    get_db,
    get_http,
    get_jarvis_user_id,
)
from telegram_bot.handlers.rate_limit import rate_limit

logger = logging.getLogger(__name__)

_MAX_FOCUS_MINUTES = 480  # 8 hours — prevents resource exhaustion


@rate_limit(max_calls=10, window_seconds=60)
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/start`` — welcome message (auth-gated by pairing).

    ``/start`` goes through :func:`auth_check`. An unpaired chat is shown the
    ``/pair`` guidance; a paired chat receives the welcome message. Pairing is
    the sole bot-identity mechanism (see :func:`auth_check`).

    Parameters
    ----------
    update : Update
        Incoming Telegram update.
    context : ContextTypes.DEFAULT_TYPE
        Bot context.
    """
    config = get_config(context)
    db_pool = get_db(context)
    authorized, jarvis_user_id = await auth_check(update, config, db_pool)
    if not authorized:
        chat_id = update.effective_chat.id if update.effective_chat else "unknown"
        logger.warning("Unauthorised /start attempt from chat_id=%s", chat_id)
        if update.message is not None:
            await update.message.reply_text(
                "🔗 Link your JARVIS account first: open the dashboard → "
                "Settings → Integrations → Telegram, then run /pair <code>."
            )
        return
    if context.user_data is not None:
        context.user_data["jarvis_user_id"] = jarvis_user_id

    if update.message is None:
        return
    text = (
        "Welcome to <b>JARVIS RD Assistant</b>!\n\n"
        "I help you manage research papers, flashcard reviews, and projects.\n\n" + format_help()
    )
    await update.message.reply_text(text, parse_mode="HTML")


@rate_limit(max_calls=5, window_seconds=60)
@auth_required
async def help_command(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/help`` — display available commands.

    Parameters
    ----------
    update : Update
        Incoming Telegram update.
    _context : ContextTypes.DEFAULT_TYPE
        Bot context (unused — ``/help`` only formats static text).
    """
    if update.message is None:
        return
    await update.message.reply_text(format_help(), parse_mode="HTML")


@rate_limit(max_calls=1, window_seconds=60, cooldown_seconds=300)
@auth_required
async def pulse_now_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/pulse_now`` — trigger immediate Pulse generation.

    Parameters
    ----------
    update : Update
        Incoming Telegram update.
    context : ContextTypes.DEFAULT_TYPE
        Bot context.
    """
    if update.message is None:
        return
    http = get_http(context)
    config = get_config(context)
    jarvis_user_id = get_jarvis_user_id(context)
    assert jarvis_user_id is not None  # noqa: S101 — guaranteed by @auth_required
    try:
        await services_client.trigger_pulse_generation(http, config, jarvis_user_id)
    except Exception:
        logger.exception("Failed to trigger Pulse generation")
        await update.message.reply_text(
            "Failed to trigger Pulse generation. Try again later.",
            parse_mode="HTML",
        )
        return

    await update.message.reply_text(
        "⚡ Pulse generation started. Check back in a few minutes.",
        parse_mode="HTML",
    )


@rate_limit(max_calls=3, window_seconds=60)
@auth_required
async def focus_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/focus [duration]`` — start a focus session."""
    if update.message is None or update.effective_chat is None:
        return
    args = context.args or []
    try:
        minutes = min(int(args[0]) if args else 25, _MAX_FOCUS_MINUTES)
    except ValueError:
        await update.message.reply_text(
            "Please provide a valid integer for duration.",
            parse_mode="HTML",  # noqa: E501
        )
        return
    if minutes <= 0:
        await update.message.reply_text(
            "Duration must be at least 1 minute. Usage: <code>/focus [minutes]</code> (1–480).",
            parse_mode="HTML",
        )
        return

    jarvis_user_id = get_jarvis_user_id(context)
    assert jarvis_user_id is not None  # noqa: S101 -- guaranteed by @auth_required
    try:
        await services_client.start_focus_session(
            get_http(context),
            get_config(context),
            jarvis_user_id,
            minutes * 60,
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 409:
            await update.message.reply_text(
                "A focus session is already active. "
                "Use the Web timer to pause, resume, or stop it.",
                parse_mode="HTML",
            )
            return
        logger.exception("Failed to start focus session")
        await update.message.reply_text(
            "The focus session could not be started. Try again later.",
            parse_mode="HTML",
        )
        return
    except Exception:
        logger.exception("Failed to start focus session")
        await update.message.reply_text(
            "The focus session could not be started. Try again later.",
            parse_mode="HTML",
        )
        return

    await update.message.reply_text(
        f"Focus session started for {minutes} minutes. Scheduled notifications are paused.",
        parse_mode="HTML",
    )
