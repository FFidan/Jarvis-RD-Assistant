"""Author alert workflow: notify when tracked authors publish new papers."""

import logging

import asyncpg
import httpx
from telegram import Bot

from telegram_bot import services_client
from telegram_bot.config import BotConfig
from telegram_bot.formatters import format_author_alert

logger = logging.getLogger(__name__)


async def run_author_alerts(
    http_client: httpx.AsyncClient,
    db_pool: asyncpg.Pool,
    bot: Bot,
    config: BotConfig,
) -> None:
    """Check tracked authors against recent papers and send alerts.

    Delegates all matching, per-user dedup, and ``last_checked_at`` bookkeeping
    to the Paper Ingestion service via ``services_client.check_authors`` (one
    call per paired user).  ``db_pool`` is used only to list pairings; each
    pairing's REST call is wrapped so one user's backend error does not abort
    the whole run.

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
    from telegram_bot.owner import list_user_pairings

    pairings = await list_user_pairings(db_pool)
    if not pairings:
        logger.warning(
            "author_alerts skipped: no Telegram pairings exist — use /pair in Telegram to set up"
        )
        return

    for pairing in pairings:
        try:
            result = await services_client.check_authors(http_client, config, pairing.user_id)
        except Exception:
            logger.exception(
                "Failed to check tracked authors for user_id=%s (chat_id=%d)",
                pairing.user_id,
                pairing.chat_id,
            )
            continue

        for match in result.get("matches", []):
            author_name = match["author_name"]
            papers = match["papers"]
            message = format_author_alert(author_name, papers)
            try:
                await bot.send_message(
                    chat_id=pairing.chat_id,
                    text=message,
                    parse_mode="HTML",
                )
                logger.info(
                    "Author alert sent to chat_id=%d user_id=%s: %s (%d papers)",
                    pairing.chat_id,
                    pairing.user_id,
                    author_name,
                    len(papers),
                )
            except Exception:
                logger.exception(
                    "Failed to send author alert for %s to chat_id=%d",
                    author_name,
                    pairing.chat_id,
                )

    logger.info("Author alerts check complete: %d pairings checked", len(pairings))
