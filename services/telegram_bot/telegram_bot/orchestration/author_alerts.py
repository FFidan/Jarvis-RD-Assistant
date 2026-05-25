"""Author alert workflow: notify when tracked authors publish new papers."""

import logging
from datetime import UTC, datetime

import asyncpg
import httpx
from jarvis_common import author_matches
from jarvis_common.db_helpers import record_author_alert
from telegram import Bot

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
            "author_alerts skipped: no Telegram pairings exist — use /pair in Telegram to set up"
        )
        return

    # Fetch recent papers once — shared across all users to avoid N redundant queries.
    async with db_pool.acquire() as conn:
        recent_papers = await conn.fetch(
            """SELECT id, title, authors, abstract, url, source_type,
                      published_date, metadata
            FROM papers
            WHERE created_at >= NOW() - INTERVAL '24 hours'"""
        )
    if not recent_papers:
        logger.info("No recent papers to check against tracked authors")
        return

    total_authors_checked = 0
    for pairing in pairings:
        # Per-user: fetch only authors subscribed by this user.
        # user_id IS NOT DISTINCT FROM covers both exact match and NULL==NULL
        # (legacy single-tenant mode where all rows have NULL user_id).
        async with db_pool.acquire() as conn:
            authors = await conn.fetch(
                "SELECT * FROM tracked_authors"
                " WHERE enabled = TRUE AND user_id IS NOT DISTINCT FROM $1",
                pairing.user_id,
            )
        if not authors:
            logger.info(
                "No enabled tracked authors for user_id=%s (chat_id=%d)",
                pairing.user_id,
                pairing.chat_id,
            )
            continue

        total_authors_checked += len(authors)
        for author_row in authors:
            try:
                author_id = author_row["id"]
                tracked_name = author_row["author_name"]
                s2_id = author_row["s2_author_id"]
                matched_papers: list[dict] = []

                # Determine which papers match this author (pure Python, no DB)
                candidate_papers: list[asyncpg.Record] = []
                for paper in recent_papers:
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
                        candidate_papers.append(paper)

                # All DB writes for this author share one connection
                async with db_pool.acquire() as conn:
                    for paper in candidate_papers:
                        paper_id = paper["id"]
                        # Deduplicate via author_alert_log — INSERT ... ON CONFLICT is atomic
                        if await record_author_alert(
                            conn,
                            tracked_author_id=author_id,
                            paper_id=paper_id,
                            user_id=pairing.user_id,
                        ):
                            matched_papers.append(dict(paper))

                    # Update last_checked_at
                    await conn.execute(
                        "UPDATE tracked_authors SET last_checked_at = $1 WHERE id = $2",
                        datetime.now(UTC),
                        author_id,
                    )

                # Send notification to this user's chat only
                if matched_papers:
                    message = format_author_alert(tracked_name, matched_papers)
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
                            tracked_name,
                            len(matched_papers),
                        )
                    except Exception:
                        logger.exception(
                            "Failed to send author alert for %s to chat_id=%d",
                            tracked_name,
                            pairing.chat_id,
                        )
            except Exception:
                logger.exception(
                    "Error processing author %s", author_row.get("author_name", "unknown")
                )
                continue

    logger.info("Author alerts check complete: %d authors checked", total_authors_checked)
