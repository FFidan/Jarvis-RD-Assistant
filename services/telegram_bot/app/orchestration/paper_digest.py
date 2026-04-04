"""Weekly paper digest workflow."""

import logging

import asyncpg
import httpx
from telegram import Bot

from app.config import BotConfig
from app.formatters import (
    confidence_badge,
    escape,
    format_weekly_digest,
    safe_url,
    truncate,
)

logger = logging.getLogger(__name__)


async def _fetch_digest_from_api(
    http_client: httpx.AsyncClient,
    config: BotConfig,
) -> dict | None:
    """Call the paper_ingestion digest endpoint.

    Parameters
    ----------
    http_client : httpx.AsyncClient
        Shared HTTP client.
    config : BotConfig
        Bot configuration (for service URL and API key).

    Returns
    -------
    dict or None
        Parsed digest payload, or ``None`` on failure.
    """
    headers: dict[str, str] = {}
    if config.jarvis_api_key:
        headers["X-API-Key"] = config.jarvis_api_key
    try:
        resp = await http_client.get(
            f"{config.paper_ingestion_url}/api/digest/weekly",
            params={"days": 7},
            headers=headers,
            timeout=90.0,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception:
        logger.exception("Failed to fetch digest from paper_ingestion API")
        return None


async def _send_chunked(bot: Bot, chat_id: int, lines: list[str]) -> None:
    """Split lines into Telegram-safe chunks and send them.

    Parameters
    ----------
    bot : Bot
        Telegram bot instance.
    chat_id : int
        Target Telegram chat ID.
    lines : list[str]
        Message lines to send.
    """
    full_text = "\n".join(lines)
    if len(full_text) <= 3900:
        try:
            logger.debug(
                "Sending weekly digest single message to chat %s (%d chars)",
                chat_id,
                len(full_text),
            )
            await bot.send_message(
                chat_id=chat_id,
                text=truncate(full_text),
                parse_mode="HTML",
            )
        except Exception:
            logger.exception(
                "Failed sending weekly digest single message to chat %s",
                chat_id,
            )
            raise
        return

    chunks: list[str] = []
    current = ""
    for line in lines:
        if len(current) + len(line) + 1 > 3900:
            chunks.append(current)
            current = line
        else:
            current += "\n" + line if current else line
    if current:
        chunks.append(current)

    for idx, chunk in enumerate(chunks, 1):
        try:
            logger.debug(
                "Sending weekly digest chunk %d/%d to chat %s (%d chars)",
                idx,
                len(chunks),
                chat_id,
                len(chunk),
            )
            await bot.send_message(
                chat_id=chat_id,
                text=truncate(chunk),
                parse_mode="HTML",
            )
        except Exception:
            logger.exception(
                "Failed sending weekly digest chunk %d/%d to chat %s",
                idx,
                len(chunks),
                chat_id,
            )
            raise


async def _simple_digest(
    db_pool: asyncpg.Pool,
    bot: Bot,
    config: BotConfig,
) -> None:
    """Fallback: send a simple digest built from direct DB queries.

    Parameters
    ----------
    db_pool : asyncpg.Pool
        Database connection pool.
    bot : Bot
        Telegram bot instance.
    config : BotConfig
        Bot configuration.
    """
    rows = await db_pool.fetch(
        """SELECT p.id, p.title, p.url, p.published_date, p.authors,
                  t.name as topic_name, pt.relevance_score,
                  ps.summary_brief, ps.confidence
           FROM papers p
           JOIN paper_topics pt ON p.id = pt.paper_id
           JOIN topics t ON pt.topic_id = t.id
           LEFT JOIN paper_summaries ps ON p.id = ps.paper_id
           WHERE p.created_at >= NOW() - INTERVAL '7 days'
           ORDER BY t.name, pt.relevance_score DESC NULLS LAST"""
    )

    if not rows:
        logger.info("No papers in last 7 days for digest")
        return

    # Group by topic
    topics: dict[str, list] = {}
    for row in rows:
        topic = row["topic_name"]
        if topic not in topics:
            topics[topic] = []
        topics[topic].append(row)

    lines = [f"\U0001f4f0 <b>Weekly Paper Digest</b> ({len(rows)} papers)\n"]

    for topic_name, papers in topics.items():
        lines.append(f"\n\U0001f4c2 <b>{escape(topic_name)}</b> ({len(papers)} papers)")
        for p in papers[:5]:
            title = escape(p["title"][:100])
            url = safe_url(p.get("url", ""))
            confidence = p.get("confidence", "")
            brief = ""
            if p.get("summary_brief"):
                brief = f"\n   {escape(p['summary_brief'][:150])}"
            conf_badge = ""
            if confidence:
                conf_badge = f" {confidence_badge(confidence)}"
            lines.append(f'  \u2022 <a href="{url}">{title}</a>{conf_badge}{brief}')
        if len(papers) > 5:
            lines.append(f"   <i>... and {len(papers) - 5} more</i>")

    try:
        await _send_chunked(bot, config.telegram_chat_id, lines)
    except Exception:
        logger.exception("Failed to send simple digest fallback")
        return
    logger.info(
        "Simple paper digest sent: %d papers in %d topics",
        len(rows),
        len(topics),
    )


async def run_paper_digest(
    http_client: httpx.AsyncClient,
    db_pool: asyncpg.Pool,
    bot: Bot,
    config: BotConfig,
) -> None:
    """Send a weekly digest of papers grouped by topic.

    Calls the backend ``GET /api/digest/weekly`` endpoint for LLM-powered
    cross-paper synthesis.  Falls back to a simple DB-driven digest if the
    API call fails.

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
    # Try the LLM-powered digest first
    digest = await _fetch_digest_from_api(http_client, config)

    if digest and digest.get("topics"):
        text = format_weekly_digest(digest)
        lines = text.split("\n")
        await _send_chunked(bot, config.telegram_chat_id, lines)
        logger.info(
            "LLM digest sent: %d papers in %d topics",
            digest.get("total_papers", 0),
            len(digest.get("topics", [])),
        )
        return

    # Fallback to simple digest
    logger.warning("Falling back to simple digest (API returned no data)")
    await _simple_digest(db_pool, bot, config)
