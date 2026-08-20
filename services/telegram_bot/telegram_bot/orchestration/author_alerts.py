"""Author alert workflow: notify when tracked authors publish new papers."""

import logging

import httpx
from telegram import Bot

from telegram_bot import services_client
from telegram_bot.config import BotConfig
from telegram_bot.formatters import format_author_alert
from telegram_bot.notification_policy import ScheduledNotificationPolicy
from telegram_bot.platform_client import list_user_pairings

logger = logging.getLogger(__name__)


async def run_author_alerts(
    http_client: httpx.AsyncClient,
    platform_client: httpx.AsyncClient,
    bot: Bot,
    config: BotConfig,
    *,
    delivery_policy: ScheduledNotificationPolicy | None = None,
) -> None:
    """Check tracked authors against recent papers and send alerts.

    Delegates all matching, per-user dedup, and ``last_checked_at`` bookkeeping
    to the Paper Ingestion service via ``services_client.check_authors`` (one
    call per paired user). Each pairing's REST call is wrapped so one user's
    backend error does not abort
    the whole run.

    An alert counts as delivered only once Telegram has accepted it: the
    acknowledgement that follows a successful send is what records it, so an
    alert lost to a send failure is offered again by the next check rather than
    being silently consumed.

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
            "author_alerts skipped: no Telegram pairings exist — use /pair in Telegram to set up"
        )
        return

    for pairing in pairings:
        if delivery_policy is not None and await delivery_policy.suppresses(
            pairing.user_id, "author_alert"
        ):
            continue
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
                continue

            try:
                await services_client.acknowledge_author_alerts(
                    http_client,
                    config,
                    pairing.user_id,
                    tracked_author_id=match["tracked_author_id"],
                    paper_ids=[paper["id"] for paper in papers],
                )
            except Exception:
                logger.exception(
                    "Delivered the author alert for %s to chat_id=%d but could not record it; "
                    "it will be offered again",
                    author_name,
                    pairing.chat_id,
                )

    logger.info("Author alerts check complete: %d pairings checked", len(pairings))
