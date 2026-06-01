"""Project-domain command handlers: /projects, /newproject."""

from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from telegram_bot.formatters import escape, sanitize_user_input, truncate
from telegram_bot.handlers.commands._auth import auth_required
from telegram_bot.handlers.helpers import get_db, get_jarvis_user_id
from telegram_bot.handlers.rate_limit import rate_limit
from telegram_bot.handlers.types import ProjectRow
from telegram_bot.project_manager import ProjectManager

logger = logging.getLogger(__name__)


def _project_keyboard(project_id: int | str) -> InlineKeyboardMarkup:
    """Build inline keyboard for a project listing."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Details", callback_data=f"project_detail_{project_id}"),
            ]
        ]
    )


@rate_limit(max_calls=5, window_seconds=60)
@auth_required
async def projects_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/projects`` — list all active projects with status and description."""
    if update.message is None:
        return
    db = get_db(context)
    user_id = get_jarvis_user_id(context)
    if user_id is not None:
        rows = await db.fetch(
            "SELECT id, name, status, description, deadline "
            "FROM projects WHERE status = 'active' "
            "AND user_id IS NOT DISTINCT FROM $1 ORDER BY created_at DESC",
            user_id,
        )
    else:
        rows = await db.fetch(
            "SELECT id, name, status, description, deadline "
            "FROM projects WHERE status = 'active' ORDER BY created_at DESC"
        )

    if not rows:
        await update.message.reply_text("No active projects.", parse_mode="HTML")
        return

    for row in rows:
        project: ProjectRow = {
            "id": row["id"],
            "name": row["name"],
            "status": row["status"],
            "description": row.get("description"),
            "deadline": row.get("deadline"),
        }
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


@rate_limit(max_calls=5, window_seconds=60)
@auth_required
async def newproject_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/newproject <name>`` — create a new project via ProjectManager."""
    if update.message is None:
        return
    if not context.args:
        await update.message.reply_text("Usage: /newproject &lt;name&gt;", parse_mode="HTML")
        return

    name = sanitize_user_input(" ".join(context.args), 200)
    db = get_db(context)
    user_id = get_jarvis_user_id(context)
    try:
        pm = ProjectManager(db)
        result = await pm.create_project(name, user_id=user_id)
        project_id = result["id"]
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
