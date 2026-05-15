"""Review reminder workflow: nudge user when cards are due."""

import logging

import asyncpg
import httpx
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from telegram_bot.config import BotConfig
from telegram_bot.handlers.helpers import _owner_headers

logger = logging.getLogger(__name__)


async def _send_reminder_to_chat(
    http_client: httpx.AsyncClient,
    bot: Bot,
    config: BotConfig,
    chat_id: int,
    user_id: int | None = None,
) -> None:
    """Fetch due-card stats and send a review reminder to a single chat.

    Parameters
    ----------
    http_client : httpx.AsyncClient
        Shared HTTP client.
    bot : Bot
        Telegram bot instance.
    config : BotConfig
        Bot configuration.
    chat_id : int
        Target Telegram chat ID.
    user_id : int | None
        DB user PK. When set, adds ``X-Owner-User-Id`` + ``X-API-Key`` headers
        so the backend scopes stats to that user.
    """
    headers = _owner_headers(config, user_id)

    try:
        resp = await http_client.get(
            f"{config.learning_engine_url}/api/stats",
            headers=headers,
        )
        resp.raise_for_status()
        stats = resp.json()
    except (httpx.HTTPError, KeyError, ValueError):
        logger.warning(
            "Could not fetch learning engine stats for review reminder (chat_id=%d)", chat_id
        )
        return

    due_now = stats.get("due_now", 0)
    if due_now <= 0:
        logger.info("No cards due for chat_id=%d, skipping review reminder", chat_id)
        return

    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Start Review", callback_data="start_review")]]
    )

    try:
        await bot.send_message(
            chat_id=chat_id,
            text=f"\U0001f4da You have <b>{due_now}</b> cards due for review!",
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        logger.info("Review reminder sent to chat_id=%d: %d cards due", chat_id, due_now)
    except Exception:
        logger.exception(
            "Failed to send review reminder for chat_id=%d with due cards=%d",
            chat_id,
            due_now,
        )


async def run_review_reminder(
    http_client: httpx.AsyncClient,
    db_pool: asyncpg.Pool,
    bot: Bot,
    config: BotConfig,
) -> None:
    """Send a review reminder if cards are due.

    Sprint A: iterates ``telegram_user_pairings`` and delivers per-user
    reminders by sending ``X-Owner-User-Id`` + ``X-API-Key`` headers.
    Skips with a warning when no pairings exist.

    Parameters
    ----------
    http_client : httpx.AsyncClient
        Shared HTTP client.
    db_pool : asyncpg.Pool
        Database connection pool.
    bot : Bot
        Telegram bot instance.
    config : BotConfig
        Bot configuration.
    """
    from telegram_bot.owner import list_user_pairings

    pairings = await list_user_pairings(db_pool)
    if not pairings:
        logger.warning(
            "review_reminder skipped: no Telegram pairings exist — use /pair in Telegram to set up"
        )
        return

    for pairing in pairings:
        await _send_reminder_to_chat(http_client, bot, config, pairing.chat_id, pairing.user_id)
