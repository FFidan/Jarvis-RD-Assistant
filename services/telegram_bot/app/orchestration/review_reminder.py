"""Review reminder workflow: nudge user when cards are due."""

import logging

import asyncpg
import httpx
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from app.config import BotConfig

logger = logging.getLogger(__name__)


async def run_review_reminder(
    http_client: httpx.AsyncClient,
    db_pool: asyncpg.Pool,
    bot: Bot,
    config: BotConfig,
) -> None:
    """Send a review reminder if cards are due.

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
    try:
        resp = await http_client.get(f"{config.learning_engine_url}/api/stats")
        resp.raise_for_status()
        stats = resp.json()
    except (httpx.HTTPError, KeyError, ValueError):
        logger.warning("Could not fetch learning engine stats for review reminder")
        return

    due_now = stats.get("due_now", 0)
    if due_now <= 0:
        logger.info("No cards due, skipping review reminder")
        return

    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Start Review", callback_data="start_review")]]
    )

    try:
        await bot.send_message(
            chat_id=config.telegram_chat_id,
            text=f"\U0001f4da You have <b>{due_now}</b> cards due for review!",
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        logger.info("Review reminder sent: %d cards due", due_now)
    except Exception:
        logger.exception(
            "Failed to send review reminder for chat_id=%s with due cards=%d",
            config.telegram_chat_id,
            due_now,
        )
        raise
