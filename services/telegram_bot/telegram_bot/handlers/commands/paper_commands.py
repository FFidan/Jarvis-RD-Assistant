"""Paper-domain command handlers: /papers, /stats, /briefing, /next, /inbox."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from telegram_bot.formatters import (
    _BIDI_ZW_RE,
    format_morning_briefing,
    format_paper_card,
    format_pulse_card,
    format_review_stats,
)
from telegram_bot.handlers.commands._auth import auth_required
from telegram_bot.handlers.helpers import _owner_headers, get_config, get_db, get_http
from telegram_bot.handlers.rate_limit import rate_limit

logger = logging.getLogger(__name__)


def _library_keyboard(paper_id: int | str) -> InlineKeyboardMarkup:
    """/papers Library row buttons per spec §5.3."""
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
    """/next single Pulse card buttons per spec §5.3."""
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
# DB lookup occurs (DOM-D-03: silent-drop auth must not run before rate-limiter).
@rate_limit(max_calls=5, window_seconds=60)
@auth_required
async def papers_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/papers [query]`` — search paper_ingestion API or list Library papers."""
    if update.message is None:
        return
    query = _BIDI_ZW_RE.sub("", (" ".join(context.args) if context.args else "")[:500])

    http = get_http(context)
    config = get_config(context)
    jarvis_user_id: int | None = (
        context.user_data.get("jarvis_user_id") if context.user_data is not None else None
    )
    headers = _owner_headers(config, jarvis_user_id)

    if query:
        # Search via paper_ingestion API
        try:
            resp = await http.post(
                f"{config.paper_ingestion_url}/api/search",
                json={"query": query},
                headers=headers,
                timeout=30.0,
            )
            resp.raise_for_status()
            papers = resp.json()
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
            resp = await http.get(
                f"{config.paper_ingestion_url}/api/papers/feed",
                params={"view": "library", "limit": 10},
                headers=headers,
                timeout=30.0,
            )
            resp.raise_for_status()
            data = resp.json()
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
    jarvis_user_id: int | None = (
        context.user_data.get("jarvis_user_id") if context.user_data is not None else None
    )
    try:
        resp = await http.get(
            f"{config.learning_engine_url}/api/stats",
            headers=_owner_headers(config, jarvis_user_id),
            timeout=15.0,
        )
        resp.raise_for_status()
        stats = resp.json()
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
    db = get_db(context)
    http = get_http(context)
    config = get_config(context)
    user_id: int | None = (
        context.user_data.get("jarvis_user_id") if context.user_data is not None else None
    )
    # New papers in last 24 hours — scoped to the user's library when paired.
    since = datetime.now(UTC) - timedelta(hours=24)
    if user_id is not None:
        row = await db.fetchrow(
            "SELECT COUNT(*) AS cnt FROM papers p "
            "JOIN user_library ul ON ul.paper_id = p.id "
            "WHERE ul.user_id = $1 AND p.created_at >= $2",
            user_id,
            since,
        )
    else:
        row = await db.fetchrow("SELECT COUNT(*) AS cnt FROM papers WHERE created_at >= $1", since)
    new_papers_count = row["cnt"] if row else 0

    # Due cards from learning engine
    due_cards = 0
    try:
        resp = await http.get(
            f"{config.learning_engine_url}/api/stats",
            headers=_owner_headers(config, user_id),
            timeout=15.0,
        )
        resp.raise_for_status()
        stats = resp.json()
        due_cards = stats.get("due_now", 0)
    except Exception:
        logger.exception("Failed to fetch stats for briefing")

    # In-progress tasks — scoped by user_id when paired.
    if user_id is not None:
        task_rows = await db.fetch(
            "SELECT t.title, p.name AS project_name "
            "FROM tasks t LEFT JOIN projects p ON t.project_id = p.id "
            "WHERE t.status = 'in_progress' AND t.user_id IS NOT DISTINCT FROM $1 "
            "ORDER BY t.created_at DESC LIMIT 10",
            user_id,
        )
    else:
        task_rows = await db.fetch(
            "SELECT t.title, p.name AS project_name "
            "FROM tasks t LEFT JOIN projects p ON t.project_id = p.id "
            "WHERE t.status = 'in_progress' "
            "ORDER BY t.created_at DESC LIMIT 10"
        )
    tasks = [dict(r) for r in task_rows]

    # Upcoming milestones (next 7 days) — scoped by user_id when paired.
    deadline_cutoff = datetime.now(UTC) + timedelta(days=7)
    if user_id is not None:
        milestone_rows = await db.fetch(
            "SELECT m.name, m.deadline, p.name AS project_name "
            "FROM milestones m LEFT JOIN projects p ON m.project_id = p.id "
            "WHERE m.completed = false AND m.deadline <= $1 "
            "AND m.user_id IS NOT DISTINCT FROM $2 "
            "ORDER BY m.deadline ASC LIMIT 10",
            deadline_cutoff,
            user_id,
        )
    else:
        milestone_rows = await db.fetch(
            "SELECT m.name, m.deadline, p.name AS project_name "
            "FROM milestones m LEFT JOIN projects p ON m.project_id = p.id "
            "WHERE m.completed = false AND m.deadline <= $1 "
            "ORDER BY m.deadline ASC LIMIT 10",
            deadline_cutoff,
        )
    milestones = [dict(r) for r in milestone_rows]

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
    jarvis_user_id: int | None = (
        context.user_data.get("jarvis_user_id") if context.user_data is not None else None
    )
    try:
        resp = await http.get(
            f"{config.paper_ingestion_url}/api/pulse/today",
            params={"limit": 1},
            headers=_owner_headers(config, jarvis_user_id),
            timeout=30.0,
        )
        resp.raise_for_status()
        data = resp.json()
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
    """Inbox row buttons per spec §5.3 (origin-conditional feedback)."""
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
    jarvis_user_id: int | None = (
        context.user_data.get("jarvis_user_id") if context.user_data is not None else None
    )
    try:
        resp = await http.get(
            f"{config.paper_ingestion_url}/api/papers/feed",
            params={"view": "inbox", "limit": 10},
            headers=_owner_headers(config, jarvis_user_id),
            timeout=30.0,
        )
        resp.raise_for_status()
        data = resp.json()
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
