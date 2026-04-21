"""Daily briefing workflow: combined morning overview."""

import logging

import asyncpg
import httpx
from telegram import Bot

from telegram_bot.config import BotConfig
from telegram_bot.formatters import format_morning_briefing

logger = logging.getLogger(__name__)


async def run_daily_briefing(
    http_client: httpx.AsyncClient,
    db_pool: asyncpg.Pool,
    bot: Bot,
    config: BotConfig,
) -> None:
    """Send a combined morning briefing with papers, cards, and tasks.

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
    from telegram_bot.owner import resolve_owner_chat_id

    owner = await resolve_owner_chat_id(db_pool, config)
    if owner is None:
        logger.info("Skipping daily briefing: no telegram owner paired")
        return

    # New papers in last 24h + in-progress tasks + upcoming milestones — one connection
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT COUNT(*) as count FROM papers WHERE created_at >= NOW() - INTERVAL '24 hours'"
        )
        new_papers_count = row["count"] if row else 0

        # In-progress tasks
        tasks = await conn.fetch(
            """SELECT t.title, p.name as project_name
            FROM tasks t
            LEFT JOIN projects p ON t.project_id = p.id
            WHERE t.status = 'in_progress'
            ORDER BY t.priority
            LIMIT 10"""
        )

        # Upcoming milestones (next 7 days)
        milestones = await conn.fetch(
            """SELECT m.name, m.deadline, p.name as project_name
            FROM milestones m
            LEFT JOIN projects p ON m.project_id = p.id
            WHERE m.completed = FALSE AND m.deadline <= NOW() + INTERVAL '7 days'
            ORDER BY m.deadline"""
        )

    # Due cards from learning engine
    due_cards = 0
    try:
        resp = await http_client.get(f"{config.learning_engine_url}/api/stats")
        resp.raise_for_status()
        stats = resp.json()
        due_cards = stats.get("due_now", 0)
    except (httpx.HTTPError, KeyError, ValueError):
        logger.warning("Could not fetch learning engine stats")

    message = format_morning_briefing(
        new_papers_count,
        due_cards,
        [dict(t) for t in tasks],
        [dict(m) for m in milestones],
    )

    try:
        await bot.send_message(chat_id=owner, text=message, parse_mode="HTML")
        logger.info("Daily briefing sent")
    except Exception:  # noqa: BLE001 — top-level send; must not crash the scheduler
        logger.exception("Failed to send message")
