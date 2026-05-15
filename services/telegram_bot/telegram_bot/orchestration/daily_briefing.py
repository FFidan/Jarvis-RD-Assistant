"""Daily briefing workflow: combined morning overview."""

import logging

import asyncpg
import httpx
from telegram import Bot

from telegram_bot.config import BotConfig
from telegram_bot.formatters import format_morning_briefing

logger = logging.getLogger(__name__)


def _build_headers(config: BotConfig, user_id: int | None) -> dict[str, str]:
    """Build backend auth headers (X-API-Key + optional X-Owner-User-Id)."""
    headers: dict[str, str] = {}
    if config.jarvis_api_key:
        headers["X-API-Key"] = config.jarvis_api_key.get_secret_value()
    if user_id is not None:
        headers["X-Owner-User-Id"] = str(user_id)
    return headers


async def _run_briefing_for_chat(
    http_client: httpx.AsyncClient,
    db_pool: asyncpg.Pool,
    bot: Bot,
    config: BotConfig,
    chat_id: int,
    user_id: int | None = None,
) -> None:
    """Send the daily briefing to a single chat.

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
    chat_id : int
        Target Telegram chat ID.
    user_id : int | None
        DB user PK for scoping paper queries. None = single-tenant (matches NULL rows).
    """
    async with db_pool.acquire() as conn:
        # New papers in the last 24 hours — scoped to the user's library when
        # user_id is set (Sprint B canonical-corpus model: membership lives in
        # user_library, not papers.user_id).  Falls back to a sitewide count
        # for legacy single-tenant mode (user_id=None).
        if user_id is not None:
            row = await conn.fetchrow(
                """SELECT COUNT(*) AS count
                   FROM papers p
                   JOIN user_library ul ON ul.paper_id = p.id
                   WHERE ul.user_id = $1
                     AND p.created_at >= NOW() - INTERVAL '24 hours'""",
                user_id,
            )
        else:
            row = await conn.fetchrow(
                "SELECT COUNT(*) AS count FROM papers"
                " WHERE created_at >= NOW() - INTERVAL '24 hours'"
            )
        new_papers_count = row["count"] if row else 0

        # In-progress tasks — scoped by user_id when available
        if user_id is not None:
            tasks = await conn.fetch(
                """SELECT t.title, p.name as project_name
                FROM tasks t
                LEFT JOIN projects p ON t.project_id = p.id
                WHERE t.status = 'in_progress'
                  AND t.user_id IS NOT DISTINCT FROM $1
                ORDER BY t.priority
                LIMIT 10""",
                user_id,
            )
        else:
            tasks = await conn.fetch(
                """SELECT t.title, p.name as project_name
                FROM tasks t
                LEFT JOIN projects p ON t.project_id = p.id
                WHERE t.status = 'in_progress'
                ORDER BY t.priority
                LIMIT 10"""
            )

        # Upcoming milestones (next 7 days) — scoped to the user's milestones
        # via milestones.user_id (migration 066).  NULL milestones are system-
        # shared and visible to all users in single-tenant mode.
        if user_id is not None:
            milestones = await conn.fetch(
                """SELECT m.name, m.deadline, p.name as project_name
                FROM milestones m
                LEFT JOIN projects p ON m.project_id = p.id
                WHERE m.completed = FALSE
                  AND m.deadline <= NOW() + INTERVAL '7 days'
                  AND m.user_id IS NOT DISTINCT FROM $1
                ORDER BY m.deadline""",
                user_id,
            )
        else:
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
        resp = await http_client.get(
            f"{config.learning_engine_url}/api/stats",
            headers=_build_headers(config, user_id),
        )
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
        await bot.send_message(chat_id=chat_id, text=message, parse_mode="HTML")
        logger.info("Daily briefing sent to chat_id=%d user_id=%s", chat_id, user_id)
    except Exception:  # noqa: BLE001 — top-level send; must not crash the scheduler
        logger.exception("Failed to send daily briefing to chat_id=%d", chat_id)


async def run_daily_briefing(
    http_client: httpx.AsyncClient,
    db_pool: asyncpg.Pool,
    bot: Bot,
    config: BotConfig,
) -> None:
    """Send a combined morning briefing with papers, cards, and tasks.

    Sprint A: iterates ``telegram_user_pairings`` and delivers per-user
    briefings.  Skips with a warning when no pairings exist.

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
            "daily_briefing skipped: no Telegram pairings exist — use /pair in Telegram to set up"
        )
        return

    for pairing in pairings:
        await _run_briefing_for_chat(
            http_client, db_pool, bot, config, pairing.chat_id, pairing.user_id
        )
