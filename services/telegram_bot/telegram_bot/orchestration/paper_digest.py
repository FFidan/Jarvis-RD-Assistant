"""Weekly paper digest workflow."""

import html
import logging
import re

import asyncpg
import httpx
from telegram import Bot

from telegram_bot import owner as _owner
from telegram_bot import services_client
from telegram_bot.config import BotConfig
from telegram_bot.formatters import (
    MAX_MESSAGE_LENGTH,
    TRUNCATION_HEADROOM,
    format_weekly_digest,
    truncate,
)
from telegram_bot.notification_policy import ScheduledNotificationPolicy

# HTML tags supported by Telegram's HTML parse mode that can span text.
_OPEN_TAG_RE = re.compile(r"<(b|i|u|s|a|code|pre|tg-spoiler)(?:\s[^>]*)?>", re.IGNORECASE)
_CLOSE_TAG_RE = re.compile(r"</(b|i|u|s|a|code|pre|tg-spoiler)>", re.IGNORECASE)

# Largest chunk `truncate` passes through unmodified (4096 - 100 = 3996).  A
# balanced chunk at or below this is sent byte-for-byte intact; anything above
# is hard-cut by the send-time `truncate`, which could slice the HTML mid-tag
# or strip the closers `_balance_chunk` just appended (M12b).
_SAFE_CHUNK_LIMIT = MAX_MESSAGE_LENGTH - TRUNCATION_HEADROOM

# Budget for pre-truncating an oversized raw chunk before re-balancing: leaves
# room under _SAFE_CHUNK_LIMIT for truncate's "... (truncated)" marker plus the
# tag openers/closers `_balance_chunk` adds around the chunk.
_PRE_TRUNCATE_BUDGET = 3800


def _balance_chunk(chunk: str, open_stack: list[str]) -> tuple[str, list[str]]:
    """Return a tag-balanced version of *chunk* given *open_stack* on entry.

    Scans the chunk for opening and closing HTML tags, building a stack of
    unclosed openers.  On exit the chunk is augmented:

    * any tags still open from the *incoming* stack are prepended as bare
      openers (``<b>``, ``<a href="...">``), and
    * any tags still unclosed at the end of the chunk are appended as closers
      (``</b>``, ``</a>``).

    Returns the balanced chunk text and the updated open stack to be passed to
    the next chunk.

    Parameters
    ----------
    chunk : str
        Raw chunk text (may contain HTML tags).
    open_stack : list[str]
        Tags opened in previous chunks that have not yet been closed, in LIFO
        order (most-recently opened last).  Each entry is the full opening tag
        string, e.g. ``'<b>'`` or ``'<a href="...">'``.

    Returns
    -------
    tuple[str, list[str]]
        ``(balanced_chunk, updated_open_stack)`` where *updated_open_stack* is
        the carry-forward state for the next chunk.
    """
    # Prepend re-openers for tags inherited from the previous chunk.
    prefix = "".join(open_stack)

    # Seed with inherited openers (TG-BUG-02) so a chunk that closes — or never
    # touches — a tag opened in a prior chunk balances it: the suffix closes
    # everything still open at chunk end, making each chunk self-contained valid
    # HTML (Telegram parses every message in isolation).
    local_stack: list[str] = list(open_stack)
    pos = 0
    while pos < len(chunk):
        open_match = _OPEN_TAG_RE.search(chunk, pos)
        close_match = _CLOSE_TAG_RE.search(chunk, pos)

        # Determine which match comes first.
        if open_match and (not close_match or open_match.start() <= close_match.start()):
            local_stack.append(open_match.group(0))
            pos = open_match.end()
        elif close_match:
            tag_name = close_match.group(1).lower()
            # Pop the most recent matching opener (handles interleaved tags
            # gracefully — just remove the last matching entry).
            for idx in range(len(local_stack) - 1, -1, -1):
                if _OPEN_TAG_RE.match(local_stack[idx]) and local_stack[idx].lower().startswith(
                    f"<{tag_name}"
                ):
                    local_stack.pop(idx)
                    break
            pos = close_match.end()
        else:
            break

    # Close any tags still open at the end of this chunk (LIFO).
    suffix = "".join(
        f"</{_OPEN_TAG_RE.match(t).group(1).lower()}>"  # type: ignore[union-attr]
        for t in reversed(local_stack)
    )

    balanced = prefix + chunk + suffix
    return balanced, local_stack


logger = logging.getLogger(__name__)


async def _fetch_digest_from_api(
    http_client: httpx.AsyncClient,
    config: BotConfig,
    user_id: int,
) -> dict | None:
    """Call the paper_ingestion digest endpoint.

    Parameters
    ----------
    http_client : httpx.AsyncClient
        Shared HTTP client.
    config : BotConfig
        Bot configuration (for service URL and API key).
    user_id : int
        DB user PK.  Adds ``X-Owner-User-Id`` header so the backend
        scopes the digest to that user's paper_user_state rows.

    Returns
    -------
    dict or None
        Parsed digest payload, or ``None`` on failure.
    """
    try:
        return await services_client.fetch_weekly_digest(http_client, config, user_id)
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

    raw_chunks: list[str] = []
    current = ""
    for line in lines:
        if len(current) + len(line) + 1 > 3900:
            # Skip the empty chunk an over-budget *first* line would produce
            # (Telegram rejects empty messages).
            if current:
                raw_chunks.append(current)
            current = line
        else:
            current += "\n" + line if current else line
    if current:
        raw_chunks.append(current)

    # Balance HTML tags across chunk boundaries so Telegram's HTML parser
    # never sees an unclosed opener or a stray closer.
    open_stack: list[str] = []
    balanced_chunks: list[str] = []
    for raw in raw_chunks:
        balanced, next_stack = _balance_chunk(raw, open_stack)
        if len(balanced) > _SAFE_CHUNK_LIMIT:
            # M12b: the send-time `truncate` would hard-cut this chunk's HTML
            # (mid-tag, or stripping the closers added above).  Cut the *raw*
            # text first — `truncate` backs out of a partially-cut tag/entity —
            # then re-balance so any tag the cut leaves open is closed again.
            raw = truncate(raw, max_length=_PRE_TRUNCATE_BUDGET + TRUNCATION_HEADROOM)
            balanced, next_stack = _balance_chunk(raw, open_stack)
        open_stack = next_stack
        balanced_chunks.append(balanced)

    for idx, chunk in enumerate(balanced_chunks, 1):
        try:
            logger.debug(
                "Sending weekly digest chunk %d/%d to chat %s (%d chars)",
                idx,
                len(balanced_chunks),
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
                len(balanced_chunks),
                chat_id,
            )
            raise


async def run_paper_digest(
    http_client: httpx.AsyncClient,
    db_pool: asyncpg.Pool,
    bot: Bot,
    config: BotConfig,
    *,
    delivery_policy: ScheduledNotificationPolicy | None = None,
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
    pairings = await _owner.list_user_pairings(db_pool)
    if not pairings:
        logger.warning(
            "paper_digest skipped: no Telegram pairings exist — use /pair in Telegram to set up"
        )
        return

    for pairing in pairings:
        if delivery_policy is not None and await delivery_policy.suppresses(
            pairing.user_id, "paper_digest"
        ):
            continue
        digest = await _fetch_digest_from_api(http_client, config, user_id=pairing.user_id)
        if digest and digest.get("topics"):
            text = format_weekly_digest(digest)
            lines = text.split("\n")
            # TG-BUG-01: Telegram cannot render a relative href, so only emit
            # the inbox deep-link when an absolute base URL is configured.
            if config.jarvis_base_url:
                lines.append(
                    "\n\U0001f4f1 "
                    f'<a href="{html.escape(config.jarvis_base_url, quote=True)}'
                    '/feed?surface=inbox&amp;filter=pulse-this-week">'
                    "View in JARVIS inbox</a>"
                )
            try:
                await _send_chunked(bot, pairing.chat_id, lines)
                logger.info(
                    "LLM digest sent to chat_id=%d (user_id=%s): %d papers in %d topics",
                    pairing.chat_id,
                    pairing.user_id,
                    digest.get("total_papers", 0),
                    len(digest.get("topics", [])),
                )
            except Exception:
                logger.exception(
                    "Failed sending weekly digest to chat %s (user %s) — skipping",
                    pairing.chat_id,
                    pairing.user_id,
                )
        else:
            logger.warning(
                "paper_digest: API returned no data for chat_id=%d (user_id=%s) — skipping",
                pairing.chat_id,
                pairing.user_id,
            )
