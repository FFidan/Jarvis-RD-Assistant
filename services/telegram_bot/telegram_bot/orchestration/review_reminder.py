"""Review reminder workflow: nudge user when cards are due."""

import logging

import httpx
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from telegram_bot import services_client
from telegram_bot.config import BotConfig
from telegram_bot.notification_policy import ScheduledNotificationPolicy
from telegram_bot.platform_client import list_user_pairings

logger = logging.getLogger(__name__)


async def _send_reminder_to_chat(
    http_client: httpx.AsyncClient,
    bot: Bot,
    config: BotConfig,
    chat_id: int,
    user_id: int,
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
    user_id : int
        DB user PK. The client auth flow exchanges its local paired-user
        marker for an assertion that scopes stats to that user.
    """
    try:
        stats = await services_client.fetch_stats(http_client, config, user_id)
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
    platform_client: httpx.AsyncClient,
    bot: Bot,
    config: BotConfig,
    *,
    delivery_policy: ScheduledNotificationPolicy | None = None,
) -> None:
    """Send a review reminder if cards are due.

    Iterates ``telegram_user_pairings`` and delivers per-user reminders
    through Telegram's route-bound backend assertion flow. Skips with a
    warning when no pairings exist.

    Parameters
    ----------
    http_client : httpx.AsyncClient
        Shared HTTP client.
    platform_client : httpx.AsyncClient
        Scoped Platform client used to list active pairings.
    bot : Bot
        Telegram bot instance.
    config : BotConfig
        Bot configuration.
    """
    pairings = await list_user_pairings(platform_client, config)
    if not pairings:
        logger.warning(
            "review_reminder skipped: no Telegram pairings exist — use /pair in Telegram to set up"
        )
        return

    for pairing in pairings:
        if delivery_policy is not None and await delivery_policy.suppresses(
            pairing.user_id, "review_reminder"
        ):
            continue
        await _send_reminder_to_chat(http_client, bot, config, pairing.chat_id, pairing.user_id)
