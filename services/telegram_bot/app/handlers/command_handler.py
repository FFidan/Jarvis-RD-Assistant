"""Command handlers for the JARVIS Telegram bot.

Implements all slash-command interactions: /start, /help, /papers, /stats,
/briefing, /projects, /tasks, /done, and /newproject.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from functools import wraps
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, ContextTypes

from app.formatters import (
    escape,
    format_help,
    format_morning_briefing,
    format_paper_card,
    format_review_stats,
    truncate,
)
from app.handlers.helpers import _auth_check, _get_config, _get_db, _get_http
from app.project_manager import ProjectManager

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def auth_required(func):
    """Decorator that rejects messages from unauthorised chats."""

    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Any:
        config = _get_config(context)
        if not _auth_check(update, config):
            chat_id = update.effective_chat.id if update.effective_chat else "unknown"
            logger.warning(
                "Unauthorised access attempt from chat_id=%s",
                chat_id,
            )
            return
        return await func(update, context)

    return wrapper


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


def _project_keyboard(project_id: int | str) -> InlineKeyboardMarkup:
    """Build inline keyboard for a project listing."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Details", callback_data=f"project_detail_{project_id}"),
            ]
        ]
    )


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


@auth_required
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/start`` — send welcome message and help text.

    Parameters
    ----------
    update : Update
        Incoming Telegram update.
    context : ContextTypes.DEFAULT_TYPE
        Bot context.
    """
    text = (
        "Welcome to <b>JARVIS RD Assistant</b>!\n\n"
        "I help you manage research papers, flashcard reviews, and projects.\n\n"
        + format_help()
    )
    await update.message.reply_text(text, parse_mode="HTML")


@auth_required
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/help`` — display available commands.

    Parameters
    ----------
    update : Update
        Incoming Telegram update.
    context : ContextTypes.DEFAULT_TYPE
        Bot context.
    """
    await update.message.reply_text(format_help(), parse_mode="HTML")


@auth_required
async def papers_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/papers [query]`` — search or list recent papers.

    Parameters
    ----------
    update : Update
        Incoming Telegram update.
    context : ContextTypes.DEFAULT_TYPE
        Bot context.
    """
    query = (" ".join(context.args) if context.args else "")[:500]

    if query:
        # Search via paper_ingestion API
        http = _get_http(context)
        config = _get_config(context)
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
        db = _get_db(context)
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
        paper_id = paper.get("id", "")
        text = format_paper_card(paper)
        await update.message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=_paper_keyboard(paper_id),
            disable_web_page_preview=True,
        )


@auth_required
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/stats`` — show learning statistics.

    Parameters
    ----------
    update : Update
        Incoming Telegram update.
    context : ContextTypes.DEFAULT_TYPE
        Bot context.
    """
    http = _get_http(context)
    config = _get_config(context)
    try:
        resp = await http.get(
            f"{config.learning_engine_url}/api/stats",
            timeout=15.0,
        )
        resp.raise_for_status()
        stats = resp.json()
    except Exception:
        logger.exception("Failed to fetch stats")
        await update.message.reply_text(
            "Failed to retrieve learning stats.", parse_mode="HTML"
        )
        return

    await update.message.reply_text(format_review_stats(stats), parse_mode="HTML")


@auth_required
async def briefing_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/briefing`` — composite morning briefing.

    Gathers new paper count (24h), due cards, in-progress tasks,
    and upcoming milestones (7 days).

    Parameters
    ----------
    update : Update
        Incoming Telegram update.
    context : ContextTypes.DEFAULT_TYPE
        Bot context.
    """
    db = _get_db(context)
    http = _get_http(context)
    config = _get_config(context)

    # New papers in last 24 hours
    since = datetime.now(UTC) - timedelta(hours=24)
    row = await db.fetchrow(
        "SELECT COUNT(*) AS cnt FROM papers WHERE created_at >= $1", since
    )
    new_papers_count = row["cnt"] if row else 0

    # Due cards from learning engine
    due_cards = 0
    try:
        resp = await http.get(
            f"{config.learning_engine_url}/api/stats", timeout=15.0
        )
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
async def projects_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/projects`` — list active projects.

    Parameters
    ----------
    update : Update
        Incoming Telegram update.
    context : ContextTypes.DEFAULT_TYPE
        Bot context.
    """
    db = _get_db(context)
    rows = await db.fetch(
        "SELECT id, name, status, description, deadline "
        "FROM projects WHERE status = 'active' ORDER BY created_at DESC"
    )

    if not rows:
        await update.message.reply_text("No active projects.", parse_mode="HTML")
        return

    for row in rows:
        project = dict(row)
        name = escape(project.get("name", ""))
        desc = escape((project.get("description") or "")[:200])
        status_emoji = {"active": "🟢", "paused": "⏸️", "completed": "✅"}.get(
            project.get("status", ""), ""
        )
        text = f"{status_emoji} <b>{name}</b>"
        if desc:
            text += f"\n{desc}"
        await update.message.reply_text(
            truncate(text),
            parse_mode="HTML",
            reply_markup=_project_keyboard(project["id"]),
        )


@auth_required
async def tasks_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/tasks [project_id]`` — list in-progress tasks.

    Parameters
    ----------
    update : Update
        Incoming Telegram update.
    context : ContextTypes.DEFAULT_TYPE
        Bot context.
    """
    db = _get_db(context)
    project_id = None
    if context.args:
        try:
            project_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text(
                "Usage: /tasks [project_id]", parse_mode="HTML"
            )
            return

    base_sql = (
        "SELECT t.id, t.title, t.status, p.name AS project_name "
        "FROM tasks t LEFT JOIN projects p ON t.project_id = p.id "
        "WHERE t.status = 'in_progress'"
    )
    if project_id is not None:
        rows = await db.fetch(
            base_sql + " AND t.project_id = $1 ORDER BY t.created_at DESC LIMIT 20",
            project_id,
        )
    else:
        rows = await db.fetch(base_sql + " ORDER BY t.created_at DESC LIMIT 20")

    if not rows:
        await update.message.reply_text("No in-progress tasks.", parse_mode="HTML")
        return

    lines = ["📋 <b>In-Progress Tasks</b>\n"]
    for row in rows:
        task = dict(row)
        title = escape(task.get("title", ""))
        project_name = escape(task.get("project_name") or "")
        task_id = task.get("id", "")
        line = f"• [{task_id}] {title}"
        if project_name:
            line += f" <i>({project_name})</i>"
        lines.append(line)

    await update.message.reply_text(
        truncate("\n".join(lines)), parse_mode="HTML"
    )


@auth_required
async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/done <task_id>`` — mark a task as complete.

    Parameters
    ----------
    update : Update
        Incoming Telegram update.
    context : ContextTypes.DEFAULT_TYPE
        Bot context.
    """
    if not context.args:
        await update.message.reply_text(
            "Usage: /done &lt;task_id&gt;", parse_mode="HTML"
        )
        return

    try:
        task_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text(
            "Task ID must be a number.", parse_mode="HTML"
        )
        return

    db = _get_db(context)
    pm = ProjectManager(db)
    result = await pm.complete_task(task_id)

    if not result:
        await update.message.reply_text(
            f"Task <b>{task_id}</b> not found.", parse_mode="HTML"
        )
    else:
        await update.message.reply_text(
            f"✅ Task <b>{task_id}</b> marked as done.", parse_mode="HTML"
        )


@auth_required
async def newproject_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/newproject <name>`` — create a new project.

    Parameters
    ----------
    update : Update
        Incoming Telegram update.
    context : ContextTypes.DEFAULT_TYPE
        Bot context.
    """
    if not context.args:
        await update.message.reply_text(
            "Usage: /newproject &lt;name&gt;", parse_mode="HTML"
        )
        return

    name = " ".join(context.args)[:200]
    db = _get_db(context)
    try:
        row = await db.fetchrow(
            "INSERT INTO projects (name, status, created_at) "
            "VALUES ($1, 'active', NOW()) RETURNING id",
            name,
        )
        project_id = row["id"]
        await update.message.reply_text(
            f"✅ Project <b>{escape(name)}</b> created (ID: {project_id}).",
            parse_mode="HTML",
        )
    except Exception:
        logger.exception("Failed to create project %r", name)
        await update.message.reply_text(
            "Failed to create project. Please try again later.",
            parse_mode="HTML",
        )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_command_handlers(app: Application) -> None:
    """Register all command handlers on the given application.

    Parameters
    ----------
    app : Application
        The ``python-telegram-bot`` Application instance.
    """
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("papers", papers_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("briefing", briefing_command))
    app.add_handler(CommandHandler("projects", projects_command))
    app.add_handler(CommandHandler("tasks", tasks_command))
    app.add_handler(CommandHandler("done", done_command))
    app.add_handler(CommandHandler("newproject", newproject_command))
