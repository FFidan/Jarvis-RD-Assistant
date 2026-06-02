"""Task-domain command handlers: /tasks, /done."""

from __future__ import annotations

import logging

import httpx
from telegram import Update
from telegram.ext import ContextTypes

from telegram_bot import services_client
from telegram_bot.formatters import escape, truncate
from telegram_bot.handlers.commands._auth import auth_required
from telegram_bot.handlers.helpers import get_config, get_http, get_jarvis_user_id
from telegram_bot.handlers.rate_limit import rate_limit
from telegram_bot.handlers.types import TaskRow

logger = logging.getLogger(__name__)


@rate_limit(max_calls=5, window_seconds=60)
@auth_required
async def tasks_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/tasks [project_id]`` — list in-progress tasks, optionally filtered by project."""
    if update.message is None:
        return
    http = get_http(context)
    config = get_config(context)
    user_id = get_jarvis_user_id(context)
    assert user_id is not None  # noqa: S101 — guaranteed by @auth_required
    project_id = None
    if context.args:
        try:
            project_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("Usage: /tasks [project_id]", parse_mode="HTML")
            return

    try:
        rows = await services_client.fetch_tasks(
            http, config, user_id, status="in_progress", project_id=project_id
        )
    except (httpx.HTTPError, ValueError, KeyError):
        logger.exception("Failed to fetch tasks")
        await update.message.reply_text("⚠️ Couldn't reach JARVIS, try again.", parse_mode="HTML")
        return

    if not rows:
        await update.message.reply_text("No in-progress tasks.", parse_mode="HTML")
        return

    lines = ["📋 <b>In-Progress Tasks</b>\n"]
    for row in rows:
        task: TaskRow = {
            "id": row["id"],
            "title": row["title"],
            "status": row["status"],
            "project_name": row.get("project_name"),
        }
        title = escape(task.get("title", ""))
        project_name = escape(task.get("project_name") or "")
        task_id = task.get("id", "")
        line = f"• [{task_id}] {title}"
        if project_name:
            line += f" <i>({project_name})</i>"
        lines.append(line)

    await update.message.reply_text(truncate("\n".join(lines)), parse_mode="HTML")


@rate_limit(max_calls=10, window_seconds=60)
@auth_required
async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/done <task_id>`` — mark a task as complete via the learning engine."""
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

    http = get_http(context)
    config = get_config(context)
    user_id = get_jarvis_user_id(context)
    assert user_id is not None  # noqa: S101 — guaranteed by @auth_required
    try:
        result = await services_client.complete_task(http, config, user_id, task_id)
    except (httpx.HTTPError, ValueError, KeyError):
        logger.exception("Failed to complete task %s", task_id)
        await update.message.reply_text("⚠️ Couldn't reach JARVIS, try again.", parse_mode="HTML")
        return

    if result is None:
        await update.message.reply_text(f"Task <b>{task_id}</b> not found.", parse_mode="HTML")
    else:
        await update.message.reply_text(
            f"✅ Task <b>{task_id}</b> marked as done.", parse_mode="HTML"
        )
