"""Paper-domain command handlers: /papers, /stats, /briefing, /next, /inbox."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from telegram_bot.formatters import (
    format_morning_briefing,
    format_paper_card,
    format_review_stats,
)
from telegram_bot.handlers.commands._auth import auth_required
from telegram_bot.handlers.helpers import get_config, get_db, get_http
from telegram_bot.handlers.rate_limit import rate_limit

logger = logging.getLogger(__name__)


def _paper_keyboard(paper_id: int | str) -> InlineKeyboardMarkup:
    """Build inline keyboard for a paper listing."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Details", callback_data=f"paper_detail_{paper_id}"),
                InlineKeyboardButton("Bookmark", callback_data=f"paper_bookmark_{paper_id}"),
            ]
        ]
    )


@auth_required
@rate_limit(max_calls=5, window_seconds=60)
async def papers_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/papers [query]`` — search paper_ingestion API or list 10 most recent papers."""
    if update.message is None:
        return
    query = (" ".join(context.args) if context.args else "")[:500]

    if query:
        # Search via paper_ingestion API
        http = get_http(context)
        config = get_config(context)
        try:
            resp = await http.post(
                f"{config.paper_ingestion_url}/api/search",
                json={"query": query},
                timeout=30.0,
            )
            resp.raise_for_status()
            papers = resp.json()
        except Exception:
            logger.exception("Paper search failed")
            await update.message.reply_text(
                "Failed to search papers. Please try again later.",
                parse_mode="HTML",
            )
            return
    else:
        # List recent papers from DB
        db = get_db(context)
        rows = await db.fetch(
            "SELECT p.id, p.title, p.authors, p.published_date, p.source_type, p.url, "
            "ps.summary_brief "
            "FROM papers p "
            "LEFT JOIN paper_summaries ps ON p.id = ps.paper_id "
            "ORDER BY p.created_at DESC LIMIT 10"
        )
        papers = [dict(r) for r in rows]

    if not papers:
        await update.message.reply_text("No papers found.", parse_mode="HTML")
        return

    for paper in papers[:10]:
        paper_id = paper.get("id")
        if not paper_id:
            continue
        text = format_paper_card(paper)
        await update.message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=_paper_keyboard(paper_id),
            disable_web_page_preview=True,
        )


@auth_required
@rate_limit(max_calls=5, window_seconds=60)
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/stats`` — fetch and display learning statistics from the learning engine."""
    if update.message is None:
        return
    http = get_http(context)
    config = get_config(context)
    try:
        resp = await http.get(
            f"{config.learning_engine_url}/api/stats",
            timeout=15.0,
        )
        resp.raise_for_status()
        stats = resp.json()
    except Exception:
        logger.exception("Failed to fetch stats")
        await update.message.reply_text("Failed to retrieve learning stats.", parse_mode="HTML")
        return

    await update.message.reply_text(format_review_stats(stats), parse_mode="HTML")


@auth_required
@rate_limit(max_calls=3, window_seconds=60)
async def briefing_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/briefing`` — composite morning briefing (papers, cards, tasks, milestones)."""
    if update.message is None:
        return
    db = get_db(context)
    http = get_http(context)
    config = get_config(context)

    # New papers in last 24 hours
    since = datetime.now(UTC) - timedelta(hours=24)
    row = await db.fetchrow("SELECT COUNT(*) AS cnt FROM papers WHERE created_at >= $1", since)
    new_papers_count = row["cnt"] if row else 0

    # Due cards from learning engine
    due_cards = 0
    try:
        resp = await http.get(f"{config.learning_engine_url}/api/stats", timeout=15.0)
        resp.raise_for_status()
        stats = resp.json()
        due_cards = stats.get("due_now", 0)
    except Exception:
        logger.exception("Failed to fetch stats for briefing")

    # In-progress tasks
    task_rows = await db.fetch(
        "SELECT t.title, p.name AS project_name "
        "FROM tasks t LEFT JOIN projects p ON t.project_id = p.id "
        "WHERE t.status = 'in_progress' "
        "ORDER BY t.created_at DESC LIMIT 10"
    )
    tasks = [dict(r) for r in task_rows]

    # Upcoming milestones (next 7 days)
    deadline_cutoff = datetime.now(UTC) + timedelta(days=7)
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


@auth_required
@rate_limit(max_calls=5, window_seconds=60)
async def next_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/next`` — recommend the next paper to read."""
    if update.message is None:
        return
    db = get_db(context)
    from telegram_bot.formatters import escape

    row = await db.fetchrow(
        """
        SELECT pr.paper_id, pr.score, p.title
        FROM paper_recommendations pr
        JOIN papers p ON pr.paper_id = p.id
        WHERE pr.dismissed = FALSE
        ORDER BY pr.score DESC LIMIT 1
        """
    )
    if row:
        await update.message.reply_text(
            f"🧠 <b>Next Recommended Paper</b>\n\n"
            f"{escape(row['title'])} (Score: {row['score']:.2f})\n\n"
            f"Use /focus to start reading.",
            parse_mode="HTML",
            reply_markup=_paper_keyboard(row["paper_id"]),
        )
    else:
        await update.message.reply_text("No pending recommendations found.", parse_mode="HTML")


def _inbox_keyboard(paper_id: int | str) -> InlineKeyboardMarkup:
    """Build inline keyboard for an inbox paper card (Save / Dismiss / Read more)."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("💾 Save", callback_data=f"paper:save:{paper_id}"),
                InlineKeyboardButton("🗑 Dismiss", callback_data=f"paper:dismiss:{paper_id}"),
                InlineKeyboardButton("📖 Read more", callback_data=f"paper_detail_{paper_id}"),
            ]
        ]
    )


@auth_required
@rate_limit(max_calls=5, window_seconds=60)
async def inbox_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/inbox`` — show top 10 unread inbox papers for triage."""
    if update.message is None:
        return
    http = get_http(context)
    config = get_config(context)
    headers: dict[str, str] = {}
    if config.jarvis_api_key:
        headers["X-API-Key"] = config.jarvis_api_key
    try:
        resp = await http.get(
            f"{config.paper_ingestion_url}/api/papers/feed",
            params={"view": "inbox", "limit": 10},
            headers=headers,
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
            reply_markup=_inbox_keyboard(paper_id),
            disable_web_page_preview=True,
        )
