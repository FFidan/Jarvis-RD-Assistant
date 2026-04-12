"""Inline-keyboard callback handlers for the JARVIS Telegram bot.

Handles non-review callbacks triggered by inline keyboard buttons on
paper listings, project listings, and task actions.
"""

from __future__ import annotations

import logging
import re

from telegram import Message, Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

from app.formatters import format_paper_detail, format_project_status
from app.handlers.helpers import _auth_check, _get_config, _get_db, _get_http
from app.project_manager import ProjectManager

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Callback handlers
# ---------------------------------------------------------------------------


async def paper_detail_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``paper_detail_{id}`` — show detailed paper view.

    Fetches the paper from the paper_ingestion API and sends a
    detailed formatted message.

    Parameters
    ----------
    update : Update
        Incoming Telegram update.
    context : ContextTypes.DEFAULT_TYPE
        Bot context.
    """
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    if not isinstance(query.message, Message):
        return

    config = _get_config(context)
    db_pool = _get_db(context)
    if not await _auth_check(update, config, db_pool):
        return

    match = re.search(r"paper_detail_(\d+)", query.data)
    if not match:
        return
    paper_id = int(match.group(1))

    http = _get_http(context)
    try:
        resp = await http.get(
            f"{config.paper_ingestion_url}/api/papers/{paper_id}",
            timeout=15.0,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        logger.exception("Failed to fetch paper detail for id=%s", paper_id)
        await query.message.reply_text("Failed to load paper details.", parse_mode="HTML")
        return

    paper = data.get("paper", {}) if isinstance(data, dict) else {}
    summary = data.get("summary") if isinstance(data, dict) else None
    text = format_paper_detail(paper, summary)
    await query.message.reply_text(text, parse_mode="HTML", disable_web_page_preview=True)


async def paper_bookmark_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``paper_bookmark_{id}`` — bookmark (star) a paper.

    Upserts a row in ``paper_user_state`` with status ``'starred'``.

    Parameters
    ----------
    update : Update
        Incoming Telegram update.
    context : ContextTypes.DEFAULT_TYPE
        Bot context.
    """
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    if not isinstance(query.message, Message):
        return

    config = _get_config(context)
    db_pool = _get_db(context)
    if not await _auth_check(update, config, db_pool):
        return

    match = re.search(r"paper_bookmark_(\d+)", query.data)
    if not match:
        return
    paper_id = int(match.group(1))

    db = _get_db(context)
    try:
        await db.execute(
            "INSERT INTO paper_user_state (paper_id, status) "
            "VALUES ($1, 'starred') "
            "ON CONFLICT (paper_id) DO UPDATE SET status = 'starred'",
            paper_id,
        )
        await query.message.reply_text(f"⭐ Paper <b>{paper_id}</b> bookmarked.", parse_mode="HTML")
    except Exception:
        logger.exception("Failed to bookmark paper id=%s", paper_id)
        await query.message.reply_text("Failed to bookmark paper.", parse_mode="HTML")


async def project_detail_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``project_detail_{id}`` — show detailed project status.

    Queries the project record together with its tasks and milestones,
    then formats via ``format_project_status``.

    Parameters
    ----------
    update : Update
        Incoming Telegram update.
    context : ContextTypes.DEFAULT_TYPE
        Bot context.
    """
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    if not isinstance(query.message, Message):
        return

    config = _get_config(context)
    db_pool = _get_db(context)
    if not await _auth_check(update, config, db_pool):
        return

    match = re.search(r"project_detail_(\d+)", query.data)
    if not match:
        return
    project_id = int(match.group(1))

    db = _get_db(context)

    project_row = await db.fetchrow(
        "SELECT id, name, status, description, deadline FROM projects WHERE id = $1",
        project_id,
    )
    if not project_row:
        await query.message.reply_text("Project not found.", parse_mode="HTML")
        return

    project = dict(project_row)

    task_rows = await db.fetch(
        "SELECT id, title, status FROM tasks WHERE project_id = $1 ORDER BY created_at",
        project_id,
    )
    tasks = [dict(r) for r in task_rows]

    milestone_rows = await db.fetch(
        "SELECT id, name, deadline, completed FROM milestones "
        "WHERE project_id = $1 ORDER BY deadline",
        project_id,
    )
    milestones = [dict(r) for r in milestone_rows]

    text = format_project_status(project, tasks, milestones)
    await query.message.reply_text(text, parse_mode="HTML")


async def start_review_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``start_review`` — sent by the review reminder inline button.

    Replies with a prompt to use the /review command, since the review
    flow is managed by a ConversationHandler that must be triggered via command.
    """
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    if not isinstance(query.message, Message):
        return

    config = _get_config(context)
    db_pool = _get_db(context)
    if not await _auth_check(update, config, db_pool):
        return

    await query.message.reply_text(
        "📚 Use /review to start your flashcard review session.",
        parse_mode="HTML",
    )


async def task_done_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``task_done_{id}`` — mark a task as complete.

    Parameters
    ----------
    update : Update
        Incoming Telegram update.
    context : ContextTypes.DEFAULT_TYPE
        Bot context.
    """
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    if not isinstance(query.message, Message):
        return

    config = _get_config(context)
    db_pool = _get_db(context)
    if not await _auth_check(update, config, db_pool):
        return

    match = re.search(r"task_done_(\d+)", query.data)
    if not match:
        return
    task_id = int(match.group(1))

    db = _get_db(context)
    pm = ProjectManager(db)
    result = await pm.complete_task(task_id)

    if not result:
        await query.message.reply_text(f"Task <b>{task_id}</b> not found.", parse_mode="HTML")
    else:
        await query.message.reply_text(
            f"✅ Task <b>{task_id}</b> marked as done.", parse_mode="HTML"
        )


_PULSE_RATING_LABEL = {
    "up": "\U0001f44d Rated up",
    "down": "\U0001f44e Rated down",
    "save": "\U0001f4be Saved",
}


async def pulse_rating_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``pulse_{up,down,save}_{id}`` — record a Pulse card rating.

    POSTs to ``/api/pulse/rate`` on paper_ingestion and answers the callback
    query with a short confirmation (or an error on failure).
    """
    query = update.callback_query
    if query is None:
        return
    config = _get_config(context)
    db_pool = _get_db(context)
    if not await _auth_check(update, config, db_pool):
        await query.answer()
        return

    match = re.fullmatch(r"pulse_(up|down|save)_(\d+)", query.data or "")
    if not match:
        await query.answer(text="Invalid rating")
        return
    rating = match.group(1)
    paper_id = int(match.group(2))

    http = _get_http(context)
    headers: dict[str, str] = {}
    if config.jarvis_api_key:
        headers["X-API-Key"] = config.jarvis_api_key
    try:
        resp = await http.post(
            f"{config.paper_ingestion_url}/api/pulse/rate",
            json={"paper_id": paper_id, "rating": rating},
            headers=headers,
            timeout=15.0,
        )
        resp.raise_for_status()
    except Exception:
        logger.exception("Failed to rate Pulse card id=%s rating=%s", paper_id, rating)
        await query.answer(text="Rating failed — try again later")
        return

    await query.answer(text=_PULSE_RATING_LABEL.get(rating, "Rated"))


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_callback_handlers(app: Application) -> None:
    """Register all callback query handlers on the given application.

    Parameters
    ----------
    app : Application
        The ``python-telegram-bot`` Application instance.
    """
    app.add_handler(CallbackQueryHandler(paper_detail_callback, pattern=r"^paper_detail_\d+$"))
    app.add_handler(CallbackQueryHandler(paper_bookmark_callback, pattern=r"^paper_bookmark_\d+$"))
    app.add_handler(CallbackQueryHandler(project_detail_callback, pattern=r"^project_detail_\d+$"))
    app.add_handler(CallbackQueryHandler(task_done_callback, pattern=r"^task_done_\d+$"))
    app.add_handler(CallbackQueryHandler(start_review_callback, pattern=r"^start_review$"))
    app.add_handler(
        CallbackQueryHandler(pulse_rating_callback, pattern=r"^pulse_(up|down|save)_\d+$")
    )
