"""System command handlers: /start, /help, /focus."""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from telegram_bot.config import _owner_headers
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
                "Settings → Integrations → Telegram, then run /pair <token>."
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
    try:
        resp = await http.post(
            f"{config.paper_ingestion_url}/api/pulse/generate",
            headers=_owner_headers(config, jarvis_user_id),
            timeout=15.0,
        )
        resp.raise_for_status()
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
    if context.job_queue is None:
        await update.message.reply_text(
            "Focus sessions are unavailable (job queue not initialised).",
            parse_mode="HTML",
        )
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

    chat_id = update.effective_chat.id
    jarvis_user_id = get_jarvis_user_id(context)

    async def focus_alarm(context: ContextTypes.DEFAULT_TYPE) -> None:
        job = context.job
        if job is None or job.chat_id is None:
            return
        job_minutes, job_user_id = job.data if isinstance(job.data, tuple) else (job.data, None)
        data_minutes = job_minutes if isinstance(job_minutes, int | float) else 0
        await context.bot.send_message(
            job.chat_id,
            text=f"🍅 Focus session complete ({data_minutes} minutes). Did you finish your task? Want to add any notes?",  # noqa: E501,
        )
        try:
            http = get_http(context)
            config = get_config(context)
            await http.post(
                f"{config.learning_engine_url}/api/executive/focus/log",
                json={"duration_hours": data_minutes / 60},
                headers=_owner_headers(config, job_user_id),
                timeout=10.0,
            )
        except Exception:
            logger.exception("Failed to log focus session to backend")

    # Cancel any existing focus timer for this chat
    for job in context.job_queue.get_jobs_by_name(f"focus_{chat_id}"):
        job.schedule_removal()

    context.job_queue.run_once(
        focus_alarm,
        minutes * 60,
        chat_id=chat_id,
        name=f"focus_{chat_id}",
        data=(minutes, jarvis_user_id),
    )
    await update.message.reply_text(
        f"🍅 Focus session started for {minutes} minutes. Notifications are paused.",
        parse_mode="HTML",
    )
