"""Daily briefing workflow: combined morning overview."""

import logging

import asyncpg
import httpx
from telegram import Bot

from telegram_bot import owner as _owner
from telegram_bot import services_client
from telegram_bot.config import BotConfig
from telegram_bot.formatters import format_morning_briefing

logger = logging.getLogger(__name__)


async def _run_briefing_for_chat(
    http_client: httpx.AsyncClient,
    db_pool: asyncpg.Pool,
    bot: Bot,
    config: BotConfig,
    chat_id: int,
    user_id: int,
) -> None:
    """Send the daily briefing to a single chat.

    All product data is gathered via ``services_client`` (REST), the same
    gather the ``/briefing`` command uses.  ``db_pool`` is unused here (the
    scheduler dispatches every orchestrator with the canonical
    ``(http_client, db_pool, bot, config)`` contract).

    Parameters
    ----------
    http_client : httpx.AsyncClient
        Shared HTTP client.
    db_pool : asyncpg.Pool
        Unused — kept for the scheduler's orchestrator contract.
    bot : Bot
        Telegram bot instance.
    config : BotConfig
        Bot configuration.
    chat_id : int
        Target Telegram chat ID.
    user_id : int
        DB user PK for scoping all per-user queries.
    """
    # Each section degrades independently: a transient backend failure on one
    # gather must not suppress the whole briefing (mirrors the /briefing command).
    try:
        new_papers_count = await services_client.fetch_new_paper_count(http_client, config, user_id)
    except (httpx.HTTPError, ValueError, KeyError):
        logger.warning("daily briefing: new-papers count failed for user_id=%s", user_id)
        new_papers_count = 0
    try:
        due_cards = await services_client.fetch_due_card_count(http_client, config, user_id)
    except (httpx.HTTPError, ValueError, KeyError):
        logger.warning("daily briefing: due-cards count failed for user_id=%s", user_id)
        due_cards = 0
    try:
        tasks = await services_client.fetch_tasks(
            http_client, config, user_id, status="in_progress", limit=10
        )
    except (httpx.HTTPError, ValueError, KeyError):
        logger.warning("daily briefing: tasks fetch failed for user_id=%s", user_id)
        tasks = []
    try:
        milestones = await services_client.fetch_upcoming_milestones(
            http_client, config, user_id, within_days=7
        )
    except (httpx.HTTPError, ValueError, KeyError):
        logger.warning("daily briefing: milestones fetch failed for user_id=%s", user_id)
        milestones = []

    message = format_morning_briefing(new_papers_count, due_cards, tasks, milestones)

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

    Iterates ``telegram_user_pairings`` and delivers per-user briefings.
    Skips with a warning when no pairings exist.  Each pairing's REST gather
    is wrapped so one user's backend error does not abort the whole run.

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
            "daily_briefing skipped: no Telegram pairings exist — use /pair in Telegram to set up"
        )
        return

    for pairing in pairings:
        try:
            await _run_briefing_for_chat(
                http_client, db_pool, bot, config, pairing.chat_id, pairing.user_id
            )
        except Exception:
            logger.exception(
                "Failed to build daily briefing for user_id=%s (chat_id=%d)",
                pairing.user_id,
                pairing.chat_id,
            )
            continue
