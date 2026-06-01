"""Research Pulse delivery: thin Telegram wrapper over ``/api/pulse/today``.

Stream K rewrite — the backend Pulse subsystem now owns discovery, scoring,
and deck persistence.  This orchestrator only fetches the scored deck and
sends the top N cards with inline Up/Down/Save rating buttons.
"""

from __future__ import annotations

import logging

import asyncpg
import httpx
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from telegram_bot.config import BotConfig
from telegram_bot.formatters import format_paper_card, truncate
from telegram_bot.handlers.helpers import _owner_headers

logger = logging.getLogger(__name__)

#: Max Pulse cards to deliver per Telegram run (brevity on phone screens).
PULSE_TELEGRAM_TOP_N = 5


def _pulse_keyboard(paper_id: int) -> InlineKeyboardMarkup:
    """Pulse-delivery keyboard.

    Spec §5.3 callback name convention. The legacy ``pulse_(up|down|save)_<id>``
    handler was deleted; thumbs map to the per-paper feedback flow and Save
    uses the lifecycle endpoint.
    """
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "\U0001f44d Up",
                    callback_data=f"paper:feedback_pos:{paper_id}:pulse_thumbs",
                ),
                InlineKeyboardButton(
                    "\U0001f44e Down",
                    callback_data=f"paper:feedback_neg:{paper_id}:pulse_thumbs",
                ),
                InlineKeyboardButton(
                    "\U0001f4be Save",
                    callback_data=f"paper:save:{paper_id}",
                ),
            ]
        ]
    )


async def _send(bot: Bot, chat_id: int, text: str, **kwargs) -> None:
    try:
        await bot.send_message(
            chat_id=chat_id, text=text, parse_mode="HTML", disable_web_page_preview=True, **kwargs
        )
    except Exception:  # noqa: BLE001 — must not crash the scheduler
        logger.exception("Failed to send Pulse message")


async def _deliver_pulse_to_chat(
    http_client: httpx.AsyncClient,
    bot: Bot,
    config: BotConfig,
    chat_id: int,
    user_id: int | None = None,
) -> None:
    """Fetch today's Pulse deck and deliver to a single chat.

    Parameters
    ----------
    http_client : httpx.AsyncClient
        Shared HTTP client.
    bot : Bot
        Telegram bot instance.
    config : BotConfig
        Bot configuration.
    chat_id : int
        Target Telegram chat ID.
    user_id : int | None
        DB user PK. When set, adds ``X-Owner-User-Id`` header so the backend
        returns per-user Pulse data.
    """
    headers = _owner_headers(config, user_id)

    try:
        resp = await http_client.get(
            f"{config.paper_ingestion_url}/api/pulse/today",
            headers=headers,
            timeout=30.0,
        )
        resp.raise_for_status()
        deck = resp.json() or {}
    except httpx.HTTPStatusError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            await _send(
                bot,
                chat_id,
                "\U0001f4ed No Pulse deck yet — run /pulse_now to generate one.",
            )
            return
        logger.warning("Pulse fetch failed for chat_id=%d: %s", chat_id, exc)
        await _send(bot, chat_id, "⚠️ Pulse fetch failed — try again later.")
        return
    except Exception:  # noqa: BLE001 — top-level catch-all
        logger.exception("Unexpected error fetching Pulse deck for chat_id=%d", chat_id)
        await _send(bot, chat_id, "⚠️ Pulse fetch failed — try again later.")
        return

    cards = deck.get("cards") or []
    if not cards:
        await _send(
            bot,
            chat_id,
            "\U0001f4ed No Pulse cards today — run /pulse_now to generate a fresh deck.",
        )
        return

    await _send(bot, chat_id, f"\U0001f4e1 <b>Pulse — {len(cards)} scored paper(s)</b>")
    for card in cards[:PULSE_TELEGRAM_TOP_N]:
        paper_id = card.get("paper_id")
        if paper_id is None:
            continue
        paper = {
            "title": card.get("paper_title", "Untitled"),
            "authors": card.get("paper_authors") or [],
            "url": card.get("paper_url") or "",
            "tldr": card.get("reasoning") or "",
            "source_type": "pulse",
        }
        await _send(
            bot,
            chat_id,
            truncate(format_paper_card(paper)),
            reply_markup=_pulse_keyboard(int(paper_id)),
        )


async def run_research_pulse(
    http_client: httpx.AsyncClient,
    db_pool: asyncpg.Pool,
    bot: Bot,
    config: BotConfig,
) -> None:
    """Fetch today's Pulse deck and deliver the top cards to Telegram.

    Iterates ``telegram_user_pairings`` and delivers per-user Pulse by sending
    ``X-Owner-User-Id`` + ``X-API-Key`` headers to the backend.  Skips with a
    warning when no pairings exist.

    Parameters
    ----------
    http_client : httpx.AsyncClient
        Shared HTTP client.
    db_pool : asyncpg.Pool
        Database connection pool used to list active pairings.
    bot : Bot
        Telegram bot instance.
    config : BotConfig
        Bot configuration (service URLs and API key).
    """
    from telegram_bot.owner import list_user_pairings

    pairings = await list_user_pairings(db_pool)
    if not pairings:
        logger.warning(
            "research_pulse skipped: no Telegram pairings exist — use /pair in Telegram to set up"
        )
        return

    for pairing in pairings:
        await _deliver_pulse_to_chat(http_client, bot, config, pairing.chat_id, pairing.user_id)
