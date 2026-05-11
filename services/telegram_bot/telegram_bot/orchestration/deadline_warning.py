"""Deadline warning workflow: alert on approaching milestones."""

import logging
from datetime import UTC, datetime

import asyncpg
import httpx
from telegram import Bot

from telegram_bot.config import BotConfig
from telegram_bot.formatters import escape, truncate

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
        asyncpg records with name, deadline, project_name.
    """
    lines = ["\u26a0\ufe0f <b>Deadline Warning</b>\n"]
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
) -> None:
    """Send warnings for milestones due in the next 3 days.

    Sprint A: iterates ``telegram_user_pairings`` and delivers per-user
    warnings.  Falls back to the legacy single-tenant owner when no per-user
    pairings exist.

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
    from telegram_bot.owner import list_user_pairings, resolve_owner_chat_id

    milestones = await db_pool.fetch(
        """SELECT m.name, m.deadline, p.name as project_name
        FROM milestones m
        JOIN projects p ON m.project_id = p.id
        WHERE m.completed = FALSE
          AND m.deadline <= NOW() + INTERVAL '3 days'
          AND m.deadline > NOW()
        ORDER BY m.deadline"""
    )

    if not milestones:
        logger.info("No upcoming deadlines in next 3 days")
        return

    pairings = await list_user_pairings(db_pool)
    if pairings:
        for pairing in pairings:
            await _send_deadline_warning(bot, pairing.chat_id, milestones)
        return

    # Legacy single-tenant fallback
    owner = await resolve_owner_chat_id(db_pool, config)
    if owner is None:
        logger.info("Skipping deadline warning: no telegram owner paired")
        return
    await _send_deadline_warning(bot, owner, milestones)
