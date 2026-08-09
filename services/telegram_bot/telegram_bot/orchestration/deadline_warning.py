"""Deadline warning workflow: alert on approaching milestones."""

import logging
from datetime import UTC, datetime

import asyncpg
import httpx
from telegram import Bot

from telegram_bot import owner as _owner
from telegram_bot import services_client
from telegram_bot.config import BotConfig
from telegram_bot.formatters import escape, truncate
from telegram_bot.notification_policy import ScheduledNotificationPolicy

logger = logging.getLogger(__name__)


async def _send_deadline_warning(
    bot: Bot,
    chat_id: int,
    milestones: list,
) -> None:
    """Format and send deadline warning to a single chat.

    Parameters
    ----------
    bot : Bot
        Telegram bot instance.
    chat_id : int
        Target Telegram chat ID.
    milestones : list
        Milestone dicts with name, deadline, project_name.
    """
    lines = ["⚠️ <b>Deadline Warning</b>\n"]
    now = datetime.now(UTC)

    capped = milestones[:10]
    for m in capped:
        name = escape(m["name"])
        project = escape(m["project_name"])
        deadline = m["deadline"]
        if hasattr(deadline, "date"):
            days_left = (deadline.date() - now.date()).days
        else:
            days_left = "?"
        urgency = "\U0001f534" if isinstance(days_left, int) and days_left <= 1 else "\U0001f7e1"
        lines.append(f"{urgency} <b>{name}</b> ({project})")
        lines.append(f"   Due in {days_left} day(s)")

    if len(milestones) > 10:
        lines.append(f"\n...and {len(milestones) - 10} more upcoming deadline(s).")

    try:
        await bot.send_message(
            chat_id=chat_id,
            text=truncate("\n".join(lines)),
            parse_mode="HTML",
        )
        logger.info("Deadline warning sent to chat_id=%d: %d milestones", chat_id, len(milestones))
    except Exception:
        logger.exception("Failed to send deadline warning to chat_id=%d", chat_id)


async def run_deadline_warning(
    http_client: httpx.AsyncClient,
    db_pool: asyncpg.Pool,
    bot: Bot,
    config: BotConfig,
    *,
    delivery_policy: ScheduledNotificationPolicy | None = None,
) -> None:
    """Send warnings for milestones due in the next 3 days.

    Iterates ``telegram_user_pairings`` and delivers per-user warnings scoped
    to each user's own milestones (fetched via ``services_client``).  Skips
    with a warning when no pairings exist.  ``db_pool`` is used only to list
    pairings; each pairing's REST fetch is wrapped so one user's backend error
    does not abort the whole run.

    Parameters
    ----------
    http_client : httpx.AsyncClient
        Shared HTTP client.
    db_pool : asyncpg.Pool
        Database connection pool (used only to list pairings).
    bot : Bot
        Telegram bot instance.
    config : BotConfig
        Bot configuration.
    """
    pairings = await _owner.list_user_pairings(db_pool)
    if not pairings:
        logger.warning(
            "deadline_warning skipped: no Telegram pairings exist — use /pair in Telegram to set up"
        )
        return

    for pairing in pairings:
        if delivery_policy is not None and await delivery_policy.suppresses(
            pairing.user_id, "deadline_warning"
        ):
            continue
        try:
            milestones = await services_client.fetch_upcoming_milestones(
                http_client, config, pairing.user_id, within_days=3
            )
        except Exception:
            logger.exception(
                "Failed to fetch upcoming milestones for user_id=%s (chat_id=%d)",
                pairing.user_id,
                pairing.chat_id,
            )
            continue
        if not milestones:
            logger.info(
                "No upcoming deadlines for user_id=%s (chat_id=%d)",
                pairing.user_id,
                pairing.chat_id,
            )
            continue
        await _send_deadline_warning(bot, pairing.chat_id, milestones)
