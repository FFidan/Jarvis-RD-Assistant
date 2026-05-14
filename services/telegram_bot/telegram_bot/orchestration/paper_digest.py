"""Weekly paper digest workflow."""

import logging

import asyncpg
import httpx
from telegram import Bot

from telegram_bot.config import BotConfig
from telegram_bot.formatters import format_weekly_digest, truncate

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


async def run_paper_digest(
    http_client: httpx.AsyncClient,
    db_pool: asyncpg.Pool,
    bot: Bot,
    config: BotConfig,
) -> None:
    """Send a weekly digest of papers grouped by topic.

    Calls the backend ``GET /api/digest/weekly`` endpoint for LLM-powered
    cross-paper synthesis.  Skips with a warning when no Telegram pairings exist.

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
            "paper_digest skipped: no Telegram pairings exist — use /pair in Telegram to set up"
        )
        return

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
            logger.warning(
                "paper_digest: API returned no data for chat_id=%d — skipping (was fallback)",
                pairing.chat_id,
            )
