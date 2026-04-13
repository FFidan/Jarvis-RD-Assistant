"""Author alert workflow: notify when tracked authors publish new papers."""

import logging
from datetime import UTC, datetime

import asyncpg
import httpx
from jarvis_common import author_matches
from telegram import Bot

from app.config import BotConfig
from app.formatters import format_author_alert

logger = logging.getLogger(__name__)


async def run_author_alerts(
    http_client: httpx.AsyncClient,
    db_pool: asyncpg.Pool,
    bot: Bot,
    config: BotConfig,
) -> None:
    """Check tracked authors against recent papers and send alerts.

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
    from app.owner import resolve_owner_chat_id

    owner = await resolve_owner_chat_id(db_pool, config)
    if owner is None:
        logger.info("Skipping author alerts: no telegram owner paired")
        return

    authors = await db_pool.fetch("SELECT * FROM tracked_authors WHERE enabled = TRUE")
    if not authors:
        logger.info("No enabled tracked authors")
        return

    # Fetch papers from the last 24 hours
    recent_papers = await db_pool.fetch(
        """SELECT id, title, authors, abstract, url, source_type,
                  published_date, metadata
        FROM papers
        WHERE created_at >= NOW() - INTERVAL '24 hours'"""
    )
    if not recent_papers:
        logger.info("No recent papers to check against tracked authors")
        return

    for author_row in authors:
        try:
            author_id = author_row["id"]
            tracked_name = author_row["author_name"]
            s2_id = author_row["s2_author_id"]
            matched_papers: list[dict] = []

            for paper in recent_papers:
                paper_id = paper["id"]
                paper_authors = paper["authors"] or []
                paper_metadata = paper["metadata"] or {}

                matched = False

                # Precise match: S2 author ID
                if s2_id:
                    s2_author_ids = [
                        str(entry["authorId"])
                        for entry in paper_metadata.get("s2_author_ids", [])
                        if isinstance(entry, dict) and entry.get("authorId")
                    ]
                    if s2_id in s2_author_ids:
                        matched = True

                # Fallback: name matching
                if not matched:
                    for candidate in paper_authors:
                        if author_matches(tracked_name, candidate):
                            matched = True
                            break

                if matched:
                    # Deduplicate via author_alert_log
                    row = await db_pool.fetchrow(
                        """INSERT INTO author_alert_log (tracked_author_id, paper_id)
                        VALUES ($1, $2)
                        ON CONFLICT (tracked_author_id, paper_id) DO NOTHING
                        RETURNING tracked_author_id""",
                        author_id,
                        paper_id,
                    )
                    if row:
                        matched_papers.append(dict(paper))

            # Send notification if there are new papers
            if matched_papers:
                message = format_author_alert(tracked_name, matched_papers)
                try:
                    await bot.send_message(
                        chat_id=owner,
                        text=message,
                        parse_mode="HTML",
                    )
                    logger.info(
                        "Author alert sent: %s (%d papers)",
                        tracked_name,
                        len(matched_papers),
                    )
                except Exception:
                    logger.exception("Failed to send author alert for %s", tracked_name)

            # Update last_checked_at
            await db_pool.execute(
                "UPDATE tracked_authors SET last_checked_at = $1 WHERE id = $2",
                datetime.now(UTC),
                author_id,
            )
        except Exception:
            logger.exception("Error processing author %s", author_row.get("author_name", "unknown"))
            continue

    logger.info("Author alerts check complete: %d authors checked", len(authors))
