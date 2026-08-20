"""Project-domain command handlers: /projects, /newproject."""

from __future__ import annotations

import logging

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from telegram_bot import services_client
from telegram_bot.formatters import (
    LISTING_ROWS,
    escape,
    sanitize_user_input,
    stage_header,
    truncate,
)
from telegram_bot.handlers.commands._auth import auth_required
from telegram_bot.handlers.helpers import get_config, get_http, get_jarvis_user_id
from telegram_bot.handlers.rate_limit import rate_limit
from telegram_bot.handlers.types import ProjectRow
from telegram_bot.vocabulary import (
    ARCHIVED_PROJECT_STATUS,
    project_status_emoji,
    project_status_label,
)

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
    """Handle ``/projects`` — list non-archived projects with their status labels.

    The list is not narrowed to ``active``: a paused or completed project is
    still one the user is working with, and hiding it made the command
    disagree with the project list on the web. Only archived projects — the
    ones deliberately put away — are left out.

    One message per project is sent, so the listing stops at
    :data:`~telegram_bot.formatters.LISTING_ROWS` and its header states the
    full count: an uncapped run floods the chat and can be cut short by
    Telegram's own throttling with nothing explaining the missing rows.
    """
    if update.message is None:
        return
    http = get_http(context)
    config = get_config(context)
    user_id = get_jarvis_user_id(context)
    assert user_id is not None  # noqa: S101 — guaranteed by @auth_required
    try:
        all_rows = await services_client.fetch_projects(http, config, user_id)
    except (httpx.HTTPError, ValueError, KeyError):
        logger.exception("Failed to fetch projects")
        await update.message.reply_text("⚠️ Couldn't reach JARVIS, try again.", parse_mode="HTML")
        return

    # The REST filter takes a single status, so the non-archived set is
    # selected here rather than with one request per remaining status.
    rows = [row for row in all_rows if row.get("status") != ARCHIVED_PROJECT_STATUS]

    if not rows:
        await update.message.reply_text(
            "No projects yet — archived ones are not listed.", parse_mode="HTML"
        )
        return

    listed = rows[:LISTING_ROWS]
    await update.message.reply_text(
        stage_header("📁 <b>Projects</b>", len(listed), len(rows), "projects you are working on"),
        parse_mode="HTML",
    )

    for row in listed:
        project: ProjectRow = {
            "id": row["id"],
            "name": row["name"],
            "status": row["status"],
            "description": row.get("description"),
            "deadline": row.get("deadline"),
        }
        name = escape(project.get("name", ""))
        desc = escape((project.get("description") or "")[:200])
        status = project.get("status", "")
        badge = f"{project_status_emoji(status)} ".lstrip()
        label = escape(project_status_label(status))
        text = f"{badge}<b>{name}</b>" + (f" — {label}" if label else "")
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
    """Handle ``/newproject <name>`` — create a new project via the learning engine."""
    if update.message is None:
        return
    if not context.args:
        await update.message.reply_text("Usage: /newproject &lt;name&gt;", parse_mode="HTML")
        return

    name = sanitize_user_input(" ".join(context.args), 200)
    http = get_http(context)
    config = get_config(context)
    user_id = get_jarvis_user_id(context)
    assert user_id is not None  # noqa: S101 — guaranteed by @auth_required
    try:
        result = await services_client.create_project(http, config, user_id, name=name)
        project_id = result["id"]
    except Exception:
        logger.exception("Failed to create project %r", name)
        await update.message.reply_text(
            "Failed to create project. Please try again later.",
            parse_mode="HTML",
        )
        return

    # The project exists from here on. Confirming it sits outside the guard so
    # a failed confirmation cannot tell the user the project was never created.
    await update.message.reply_text(
        f"✅ Project <b>{escape(name)}</b> created (ID: {project_id}).",
        parse_mode="HTML",
    )
