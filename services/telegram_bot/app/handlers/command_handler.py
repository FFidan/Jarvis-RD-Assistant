# DEPRECATED — use handlers.commands.*
# This module is a thin re-export stub kept for backward compatibility.
# All command implementations live in app.handlers.commands.*

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from app.formatters import escape
from app.handlers.commands import (
    briefing_command,
    focus_command,
    help_command,
    next_command,
    papers_command,
    projects_command,
    pulse_now_command,
    register_command_handlers,
    start_command,
    stats_command,
    tasks_command,
)
from app.handlers.commands._auth import auth_required
from app.handlers.commands.system_commands import _handle_pairing
from app.handlers.helpers import _get_db
from app.handlers.rate_limit import rate_limit

# Re-export ProjectManager so existing test patches targeting
# `app.handlers.command_handler.ProjectManager` continue to work.
# done_command / newproject_command live here (not in task_commands /
# project_commands) so that patching this module's ProjectManager name
# intercepts the call site — as the existing tests expect.
from app.project_manager import ProjectManager

logger = logging.getLogger(__name__)


@auth_required
@rate_limit(max_calls=10, window_seconds=60)
async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/done <task_id>`` — mark a task as complete."""
    if update.message is None:
        return
    if not context.args:
        await update.message.reply_text("Usage: /done &lt;task_id&gt;", parse_mode="HTML")
        return

    try:
        task_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Task ID must be a number.", parse_mode="HTML")
        return

    db = _get_db(context)
    pm = ProjectManager(db)
    result = await pm.complete_task(task_id)

    if not result:
        await update.message.reply_text(f"Task <b>{task_id}</b> not found.", parse_mode="HTML")
    else:
        await update.message.reply_text(
            f"✅ Task <b>{task_id}</b> marked as done.", parse_mode="HTML"
        )


@auth_required
@rate_limit(max_calls=5, window_seconds=60)
async def newproject_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/newproject <name>`` — create a new project."""
    if update.message is None:
        return
    if not context.args:
        await update.message.reply_text("Usage: /newproject &lt;name&gt;", parse_mode="HTML")
        return

    name = " ".join(context.args)[:200]
    db = _get_db(context)
    try:
        pm = ProjectManager(db)
        result = await pm.create_project(name)
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


__all__ = [
    "briefing_command",
    "done_command",
    "focus_command",
    "help_command",
    "newproject_command",
    "next_command",
    "papers_command",
    "projects_command",
    "pulse_now_command",
    "register_command_handlers",
    "start_command",
    "stats_command",
    "tasks_command",
    "_handle_pairing",
    "ProjectManager",
]
