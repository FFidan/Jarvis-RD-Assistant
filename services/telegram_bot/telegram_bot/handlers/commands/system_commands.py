"""System command handlers: /start, /help, /focus, and pairing flow."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from jarvis_common.event_log import log_event
from telegram import Update
from telegram.ext import ContextTypes

from telegram_bot.formatters import format_help
from telegram_bot.handlers.commands._auth import auth_required
from telegram_bot.handlers.helpers import auth_check, get_config, get_db, get_http
from telegram_bot.handlers.rate_limit import rate_limit

logger = logging.getLogger(__name__)

_MAX_FOCUS_MINUTES = 480  # 8 hours — prevents resource exhaustion

# Pairing rate-limit constants (inline implementation; see TODO below)
_PAIRING_RATE_WINDOW_SECONDS = 60  # sliding window duration
_PAIRING_MAX_ATTEMPTS = 5  # max pairing attempts per window
# TODO(B-followup): convert to @rate_limit decorator when promoted to public API


async def _handle_pairing(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    code: str,
) -> None:
    """Complete the dashboard-initiated Telegram pairing flow.

    Looks up ``telegram_pairing`` for the given ``code`` under a row lock.
    If the code exists and has not expired, persists the current chat id into
    ``user_config.telegram.owner_chat_id`` (as a JSON integer) and deletes the
    used code. Invalid/expired codes are also cleaned up opportunistically.

    Rate-limited to 5 attempts per 60 s per chat to prevent brute-forcing.
    """
    import hashlib
    import time

    from telegram_bot.handlers.rate_limit import _timestamps

    db_pool = get_db(context)
    chat = update.effective_chat
    message = update.message
    if chat is None or message is None:
        return

    # --- inline rate-limit: pairing attempts per sliding window per chat ---
    # TODO(B-followup): convert to @rate_limit decorator when promoted to public API
    _rl_key = f"{chat.id}:telegram_bot.handlers.commands.system_commands._handle_pairing"
    _now = time.monotonic()
    _stamps = _timestamps[_rl_key]
    _stamps[:] = [t for t in _stamps if _now - t < _PAIRING_RATE_WINDOW_SECONDS]
    if len(_stamps) >= _PAIRING_MAX_ATTEMPTS:
        logger.warning("pairing rate-limited chat_id=%s", chat.id)
        await message.reply_text(
            f"Too many pairing attempts — please wait {_PAIRING_RATE_WINDOW_SECONDS}s"
            " before trying again."
        )
        return
    _stamps.append(_now)

    code_hash = hashlib.sha256(code.encode()).hexdigest()[:8]
    try:
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                # Refuse if an owner is already paired (takeover prevention)
                current_owner = await conn.fetchval(
                    "SELECT value FROM user_config "
                    "WHERE key = 'telegram.owner_chat_id' AND user_id IS NULL"
                )
                if current_owner is not None:
                    await message.reply_text(
                        "This JARVIS instance is already paired. "
                        "Unpair from the dashboard first (Settings → Integrations)."
                    )
                    logger.info("pairing refused: owner already set, code_hash=%s", code_hash)
                    return
                row = await conn.fetchrow(
                    "SELECT expires_at FROM telegram_pairing WHERE code = $1 FOR UPDATE",
                    code,
                )
                if row is None:
                    await message.reply_text("Invalid or expired pairing code.")
                    return
                if row["expires_at"] < datetime.now(UTC):
                    await conn.execute("DELETE FROM telegram_pairing WHERE code = $1", code)
                    await message.reply_text("Invalid or expired pairing code.")
                    return
                await conn.execute(
                    "INSERT INTO user_config (user_id, key, value) "
                    "VALUES (NULL, 'telegram.owner_chat_id', $1::jsonb) "
                    "ON CONFLICT (user_id, key) DO UPDATE "
                    "SET value = EXCLUDED.value, updated_at = NOW()",
                    chat.id,
                )
                await conn.execute("DELETE FROM telegram_pairing WHERE code = $1", code)
        await log_event(
            pool=db_pool,
            level="info",
            category="config",
            source="telegram_bot",
            message="setting_changed",
            context={"chat_id": chat.id, "command": "start_pairing"},
        )
        await message.reply_text("✅ Paired! You'll now receive JARVIS notifications here.")
    except Exception:
        logger.exception("pairing_failed code_hash=%s", code_hash)  # hash only — not raw code
        await message.reply_text("Pairing failed — please try again from the dashboard.")


@rate_limit(max_calls=10, window_seconds=60)
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/start`` — pairing deep-link or welcome message.

    A Telegram deep-link of the form ``/start PAIR_<code>`` completes the
    dashboard-initiated pairing flow (sets ``user_config.telegram.owner_chat_id``
    to this chat's id) without requiring a pre-configured ``TELEGRAM_CHAT_ID``.
    This is the ONLY un-authed bot entrypoint; all other ``/start`` invocations
    still go through :func:`auth_check` before replying.

    Parameters
    ----------
    update : Update
        Incoming Telegram update.
    context : ContextTypes.DEFAULT_TYPE
        Bot context.
    """
    message = update.message
    raw_text = getattr(message, "text", None) if message is not None else None
    if isinstance(raw_text, str):
        parts = raw_text.split(maxsplit=1)
        if len(parts) > 1 and parts[1].startswith("PAIR_"):
            await _handle_pairing(update, context, parts[1][len("PAIR_") :])
            return

    config = get_config(context)
    db_pool = get_db(context)
    if not await auth_check(update, config, db_pool):
        chat_id = update.effective_chat.id if update.effective_chat else "unknown"
        logger.warning("Unauthorised /start attempt from chat_id=%s", chat_id)
        return

    if update.message is None:
        return
    text = (
        "Welcome to <b>JARVIS RD Assistant</b>!\n\n"
        "I help you manage research papers, flashcard reviews, and projects.\n\n" + format_help()
    )
    await update.message.reply_text(text, parse_mode="HTML")


@auth_required
@rate_limit(max_calls=5, window_seconds=60)
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


@auth_required
@rate_limit(max_calls=1, window_seconds=60, cooldown_seconds=300)
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
    headers = {}
    if config.jarvis_api_key:
        headers["X-API-Key"] = config.jarvis_api_key.get_secret_value()
    try:
        resp = await http.post(
            f"{config.paper_ingestion_url}/api/pulse/generate",
            headers=headers,
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


@auth_required
@rate_limit(max_calls=3, window_seconds=60)
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

    async def focus_alarm(context: ContextTypes.DEFAULT_TYPE) -> None:
        job = context.job
        if job is None or job.chat_id is None:
            return
        data_minutes = job.data if isinstance(job.data, int | float) else 0
        await context.bot.send_message(
            job.chat_id,
            text=f"🍅 Focus session complete ({data_minutes} minutes). Did you finish your task? Want to add any notes?",  # noqa: E501,
        )
        try:
            http = get_http(context)
            config = get_config(context)
            headers: dict[str, str] = {}
            if config.jarvis_api_key:
                headers["X-API-Key"] = config.jarvis_api_key.get_secret_value()
            await http.post(
                f"{config.learning_engine_url}/api/executive/focus/log",
                json={"duration_hours": data_minutes / 60},
                headers=headers,
                timeout=10.0,
            )
        except Exception:
            logger.exception("Failed to log focus session to backend")

    # Cancel any existing focus timer for this chat
    for job in context.job_queue.get_jobs_by_name(f"focus_{chat_id}"):
        job.schedule_removal()

    context.job_queue.run_once(
        focus_alarm, minutes * 60, chat_id=chat_id, name=f"focus_{chat_id}", data=minutes
    )
    await update.message.reply_text(
        f"🍅 Focus session started for {minutes} minutes. Notifications are paused.",
        parse_mode="HTML",
    )
