"""Paper-domain command handlers: /papers, /stats, /briefing, /next, /inbox."""

from __future__ import annotations

import logging

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from telegram_bot import services_client
from telegram_bot.formatters import (
    format_morning_briefing,
    format_paper_card,
    format_pulse_card,
    format_review_stats,
    sanitize_user_input,
)
from telegram_bot.handlers.commands._auth import auth_required
from telegram_bot.handlers.helpers import (
    get_config,
    get_http,
    get_jarvis_user_id,
)
from telegram_bot.handlers.rate_limit import rate_limit

logger = logging.getLogger(__name__)


def _library_keyboard(paper_id: int | str) -> InlineKeyboardMarkup:
    """/papers Library row buttons."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("⭐ Star", callback_data=f"paper:star:{paper_id}"),
                InlineKeyboardButton("🗑 Trash", callback_data=f"paper:trash:{paper_id}"),
                InlineKeyboardButton("📖 Read more", callback_data=f"paper_detail_{paper_id}"),
            ]
        ]
    )


def _pulse_card_keyboard(paper_id: int | str) -> InlineKeyboardMarkup:
    """/next single Pulse card buttons."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("💾 Save", callback_data=f"paper:save:{paper_id}"),
                InlineKeyboardButton("🗑 Trash", callback_data=f"paper:trash:{paper_id}"),
                InlineKeyboardButton("🗑+👎", callback_data=f"paper:trash_reject:{paper_id}"),
            ],
            [
                InlineKeyboardButton(
                    "👍", callback_data=f"paper:feedback_pos:{paper_id}:pulse_thumbs"
                ),
                InlineKeyboardButton(
                    "👎", callback_data=f"paper:feedback_neg:{paper_id}:pulse_thumbs"
                ),
                InlineKeyboardButton("📖 Read more", callback_data=f"paper_detail_{paper_id}"),
            ],
        ]
    )


# Decorator order: @rate_limit outer, @auth_required inner.
# Rate-limiting runs FIRST so unauthenticated floods are shed before any auth
# DB lookup occurs (auth must not run before rate-limiter).
@rate_limit(max_calls=5, window_seconds=60)
@auth_required
async def papers_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/papers [query]`` — search paper_ingestion API or list Library papers."""
    if update.message is None:
        return
    query = sanitize_user_input(" ".join(context.args) if context.args else "", 500)

    http = get_http(context)
    config = get_config(context)
    jarvis_user_id = get_jarvis_user_id(context)
    assert jarvis_user_id is not None  # noqa: S101 — guaranteed by @auth_required

    if query:
        # Search via paper_ingestion API
        try:
            papers = await services_client.search_papers(http, config, jarvis_user_id, query)
            if isinstance(papers, dict):
                papers = papers.get("papers", [])
        except Exception:
            logger.exception("Paper search failed")
            await update.message.reply_text(
                "Failed to search papers. Please try again later.",
                parse_mode="HTML",
            )
            return
    else:
        # List Library papers via feed API
        try:
            data = await services_client.fetch_papers_feed(
                http, config, jarvis_user_id, view="library", limit=10
            )
            papers = data.get("papers", []) if isinstance(data, dict) else []
        except Exception:
            logger.exception("Failed to fetch library feed")
            await update.message.reply_text(
                "Failed to load library. Please try again later.",
                parse_mode="HTML",
            )
            return

    if not papers:
        await update.message.reply_text(
            "📚 Your Library is empty. Save papers from /inbox or /next to start building it.",
            parse_mode="HTML",
        )
        return

    for paper in papers[:10]:
        paper_id = paper.get("id")
        if not paper_id:
            continue
        text = format_paper_card(paper)
        await update.message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=_library_keyboard(paper_id),
            disable_web_page_preview=True,
        )


@rate_limit(max_calls=5, window_seconds=60)
@auth_required
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/stats`` — fetch and display learning statistics from the learning engine."""
    if update.message is None:
        return
    http = get_http(context)
    config = get_config(context)
    jarvis_user_id = get_jarvis_user_id(context)
    assert jarvis_user_id is not None  # noqa: S101 — guaranteed by @auth_required
    try:
        stats = await services_client.fetch_stats(http, config, jarvis_user_id, timeout=15.0)
    except Exception:
        logger.exception("Failed to fetch stats")
        await update.message.reply_text("Failed to retrieve learning stats.", parse_mode="HTML")
        return

    await update.message.reply_text(format_review_stats(stats), parse_mode="HTML")


@rate_limit(max_calls=3, window_seconds=60)
@auth_required
async def briefing_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/briefing`` — composite morning briefing (papers, cards, tasks, milestones)."""
    if update.message is None:
        return
    http = get_http(context)
    config = get_config(context)
    user_id = get_jarvis_user_id(context)
    assert user_id is not None  # noqa: S101 — guaranteed by @auth_required

    # Each section degrades independently: a transient failure on one gather
    # leaves that section empty/zero rather than aborting the whole briefing.

    # New papers in last 24 hours.
    new_papers_count = 0
    try:
        new_papers_count = await services_client.fetch_new_paper_count(http, config, user_id)
    except (httpx.HTTPError, ValueError, KeyError):
        logger.exception("Failed to fetch new-paper count for briefing")

    # Due cards from learning engine.
    due_cards = 0
    try:
        due_cards = await services_client.fetch_due_card_count(http, config, user_id)
    except (httpx.HTTPError, ValueError, KeyError):
        logger.exception("Failed to fetch due-card count for briefing")

    # In-progress tasks.
    tasks: list[dict] = []
    try:
        tasks = await services_client.fetch_tasks(
            http, config, user_id, status="in_progress", limit=10
        )
    except (httpx.HTTPError, ValueError, KeyError):
        logger.exception("Failed to fetch tasks for briefing")

    # Upcoming milestones (next 7 days).
    milestones: list[dict] = []
    try:
        milestones = await services_client.fetch_upcoming_milestones(
            http, config, user_id, within_days=7
        )
    except (httpx.HTTPError, ValueError, KeyError):
        logger.exception("Failed to fetch milestones for briefing")

    text = format_morning_briefing(new_papers_count, due_cards, tasks, milestones)
    await update.message.reply_text(text, parse_mode="HTML")


@rate_limit(max_calls=5, window_seconds=60)
@auth_required
async def next_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/next`` — surface the top Pulse card as the next paper to read."""
    if update.message is None:
        return
    http = get_http(context)
    config = get_config(context)
    jarvis_user_id = get_jarvis_user_id(context)
    assert jarvis_user_id is not None  # noqa: S101 — guaranteed by @auth_required
    try:
        data = await services_client.fetch_pulse_today(http, config, jarvis_user_id, limit=1)
        cards = data.get("cards", []) if isinstance(data, dict) else []
    except Exception:
        logger.exception("Failed to fetch pulse deck for /next")
        await update.message.reply_text(
            "Failed to load next recommendation. Please try again later.",
            parse_mode="HTML",
        )
        return

    if not cards:
        await update.message.reply_text(
            "🌙 No Pulse deck yet — try /pulse_now to generate one.",
            parse_mode="HTML",
        )
        return

    card = cards[0]
    paper_id = card.get("paper_id") or card.get("id")
    if not paper_id:
        await update.message.reply_text("Pulse card has no paper_id.", parse_mode="HTML")
        return

    text = format_pulse_card(card)
    await update.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=_pulse_card_keyboard(paper_id),
        disable_web_page_preview=True,
    )


def _inbox_keyboard(
    paper_id: int | str, discovery_origin: str = "user_initiated"
) -> InlineKeyboardMarkup:
    """Inbox row buttons (origin-conditional feedback)."""
    primary = [
        InlineKeyboardButton("💾 Save", callback_data=f"paper:save:{paper_id}"),
        InlineKeyboardButton("🗑 Trash", callback_data=f"paper:trash:{paper_id}"),
    ]
    if discovery_origin != "user_initiated":
        primary.append(
            InlineKeyboardButton("🗑+👎", callback_data=f"paper:trash_reject:{paper_id}"),
        )
    secondary = [InlineKeyboardButton("📖 Read more", callback_data=f"paper_detail_{paper_id}")]
    if discovery_origin != "user_initiated":
        secondary = [
            InlineKeyboardButton("👍", callback_data=f"paper:feedback_pos:{paper_id}:feed_thumbs"),
            InlineKeyboardButton("👎", callback_data=f"paper:feedback_neg:{paper_id}:feed_thumbs"),
        ] + secondary
    return InlineKeyboardMarkup([primary, secondary])


@rate_limit(max_calls=5, window_seconds=60)
@auth_required
async def inbox_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/inbox`` — show top 10 unread inbox papers for triage."""
    if update.message is None:
        return
    http = get_http(context)
    config = get_config(context)
    jarvis_user_id = get_jarvis_user_id(context)
    assert jarvis_user_id is not None  # noqa: S101 — guaranteed by @auth_required
    try:
        data = await services_client.fetch_papers_feed(
            http, config, jarvis_user_id, view="inbox", limit=10
        )
    except Exception:
        logger.exception("Failed to fetch inbox feed")
        await update.message.reply_text(
            "Failed to load inbox. Please try again later.",
            parse_mode="HTML",
        )
        return

    if isinstance(data, list):
        papers = data
    elif isinstance(data, dict):
        papers = data.get("papers", [])
    else:
        papers = []
    if not papers:
        await update.message.reply_text("📭 Inbox is empty — nothing to triage.", parse_mode="HTML")
        return

    for paper in papers[:10]:
        paper_id = paper.get("id")
        if not paper_id:
            continue
        text = format_paper_card(paper)
        await update.message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=_inbox_keyboard(paper_id, paper.get("discovery_origin", "user_initiated")),
            disable_web_page_preview=True,
        )
