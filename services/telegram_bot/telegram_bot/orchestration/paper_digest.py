"""Weekly paper digest workflow."""

import logging

import asyncpg
import httpx
from jarvis_common.auth import single_tenant_user_id
from telegram import Bot

from telegram_bot.config import BotConfig
from telegram_bot.formatters import (
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
        headers["X-API-Key"] = config.jarvis_api_key.get_secret_value()
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
    _config: BotConfig,
    owner: int,
    *,
    db_user_id: int | None = None,
) -> None:
    """Fallback: send a simple digest built from direct DB queries.

    Parameters
    ----------
    db_pool : asyncpg.Pool
        Database connection pool.
    bot : Bot
        Telegram bot instance.
    _config : BotConfig
        Bot configuration (reserved for future use — not yet needed by the
        fallback query path).
    owner : int
        Resolved owner chat ID for Bot.send_message (Telegram chat ID, NOT a DB user PK).
    db_user_id : int | None, keyword-only
        DB user PK for paper_user_state / recommendation_feedback scoping. ``None``
        (default) means single-tenant mode and matches NULL rows via
        ``IS NOT DISTINCT FROM``. Multi-tenant callers must resolve the
        Telegram chat → DB user via the (yet-to-be-wired) pairing table
        and pass the concrete PK; see ARCHITECTURE.md "Authentication
        and Ownership".
    """
    # Phase A migration: digest now reads state ENUM (state IN ('trash','done')
    # for the exclude guard; state IN ('reading','done') for include) plus
    # recommendation_feedback (signal='positive', source='pulse_thumbs') for the
    # 7-day positive-feedback window. The legacy pulse_ratings table was DROPPED
    # in Wave 1cd. We duplicate the query inline rather than import from
    # paper_ingestion.queries.predicates.VIEW_PREDICATES because telegram_bot
    # is a separate deployable service.
    rows = await db_pool.fetch(
        """SELECT p.id, p.title, p.url, p.published_date, p.authors,
                  t.name as topic_name, pt.relevance_score,
                  ps.summary_brief, ps.confidence
           FROM papers p
           JOIN paper_topics pt ON p.id = pt.paper_id
           JOIN topics t ON pt.topic_id = t.id
           LEFT JOIN paper_summaries ps ON p.id = ps.paper_id
           WHERE p.created_at >= NOW() - INTERVAL '7 days'
             AND NOT EXISTS (
                 SELECT 1 FROM paper_user_state pus
                  WHERE pus.paper_id = p.id
                    AND pus.user_id IS NOT DISTINCT FROM $1
                    AND pus.state IN ('trash', 'done')
             )
             AND (
                 EXISTS (
                     SELECT 1 FROM paper_user_state pus2
                     WHERE pus2.paper_id = p.id
                       AND pus2.user_id IS NOT DISTINCT FROM $1
                       AND (
                           COALESCE(pus2.starred, FALSE) = TRUE
                           OR pus2.state = 'reading'
                       )
                 )
                 OR EXISTS (
                     SELECT 1 FROM recommendation_feedback rf
                     WHERE rf.paper_id = p.id
                       AND rf.user_id IS NOT DISTINCT FROM $1
                       AND rf.signal = 'positive'
                       AND rf.source = 'pulse_thumbs'
                       AND rf.created_at >= NOW() - INTERVAL '7 days'
                 )
             )
           ORDER BY t.name, pt.relevance_score DESC NULLS LAST""",
        db_user_id,
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

    lines.append(
        "\n\U0001f4f1 "
        '<a href="/feed?surface=inbox&amp;filter=pulse-this-week">'
        "View in JARVIS inbox</a>"
    )
    try:
        await _send_chunked(bot, owner, lines)
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
    from telegram_bot.owner import list_user_pairings, resolve_owner_chat_id

    pairings = await list_user_pairings(db_pool)
    if pairings:
        # Multi-tenant: deliver per-user digest for each pairing.
        digest = await _fetch_digest_from_api(http_client, config)
        for pairing in pairings:
            if digest and digest.get("topics"):
                text = format_weekly_digest(digest)
                lines = text.split("\n")
                lines.append(
                    "\n\U0001f4f1 "
                    '<a href="/feed?surface=inbox&amp;filter=pulse-this-week">'
                    "View in JARVIS inbox</a>"
                )
                await _send_chunked(bot, pairing.chat_id, lines)
                logger.info(
                    "LLM digest sent to chat_id=%d: %d papers in %d topics",
                    pairing.chat_id,
                    digest.get("total_papers", 0),
                    len(digest.get("topics", [])),
                )
            else:
                logger.warning("Falling back to simple digest for chat_id=%d", pairing.chat_id)
                await _simple_digest(
                    db_pool, bot, config, pairing.chat_id, db_user_id=pairing.user_id
                )
        return

    # Legacy single-tenant fallback
    owner = await resolve_owner_chat_id(db_pool, config)
    if owner is None:
        logger.info("Skipping paper digest: no telegram owner paired")
        return

    # Try the LLM-powered digest first
    digest = await _fetch_digest_from_api(http_client, config)

    if digest and digest.get("topics"):
        text = format_weekly_digest(digest)
        lines = text.split("\n")
        lines.append(
            "\n\U0001f4f1 "
            '<a href="/feed?surface=inbox&amp;filter=pulse-this-week">'
            "View in JARVIS inbox</a>"
        )
        await _send_chunked(bot, owner, lines)
        logger.info(
            "LLM digest sent: %d papers in %d topics",
            digest.get("total_papers", 0),
            len(digest.get("topics", [])),
        )
        return

    # Fallback to simple digest
    logger.warning("Falling back to simple digest (API returned no data)")
    await _simple_digest(db_pool, bot, config, owner, db_user_id=single_tenant_user_id())
