"""Inline-keyboard callback handlers for the JARVIS Telegram bot.

Handles non-review callbacks triggered by inline keyboard buttons on
paper listings, project listings, and task actions.
"""

from __future__ import annotations

import logging
import re

from telegram import Message, Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

from telegram_bot.formatters import format_paper_detail, format_project_status
from telegram_bot.handlers.helpers import auth_check, get_config, get_db, get_http
from telegram_bot.handlers.rate_limit import rate_limit
from telegram_bot.handlers.review_handler import review_start
from telegram_bot.project_manager import ProjectManager

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Callback handlers
# ---------------------------------------------------------------------------


@rate_limit(max_calls=10, window_seconds=60)
async def paper_detail_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``paper_detail_{id}`` — fetch paper from paper_ingestion API and show detail."""
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    if not isinstance(query.message, Message):
        return

    config = get_config(context)
    db_pool = get_db(context)
    if not await auth_check(update, config, db_pool):
        return

    if not query.data or not (match := re.search(r"paper_detail_(\d+)", query.data)):
        return
    paper_id = int(match.group(1))

    http = get_http(context)
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


@rate_limit(max_calls=10, window_seconds=60)
async def paper_bookmark_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``paper_bookmark_{id}`` — bookmark (star) a paper."""
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    if not isinstance(query.message, Message):
        return

    config = get_config(context)
    db_pool = get_db(context)
    if not await auth_check(update, config, db_pool):
        return

    if not query.data or not (match := re.search(r"paper_bookmark_(\d+)", query.data)):
        return
    paper_id = int(match.group(1))

    db = get_db(context)
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


@rate_limit(max_calls=10, window_seconds=60)
async def project_detail_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``project_detail_{id}`` — query project, tasks, and milestones then reply."""
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    if not isinstance(query.message, Message):
        return

    config = get_config(context)
    db_pool = get_db(context)
    if not await auth_check(update, config, db_pool):
        return

    if not query.data or not (match := re.search(r"project_detail_(\d+)", query.data)):
        return
    project_id = int(match.group(1))

    db = get_db(context)

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


@rate_limit(max_calls=5, window_seconds=60)
async def start_review_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``start_review`` — sent by the review reminder inline button.

    Bootstraps the review flow by fetching the first due card directly, since
    ConversationHandler entry cannot be triggered from a callback query.  The
    user is then prompted to type /review for subsequent cards (or to continue
    the session).
    """
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    if not isinstance(query.message, Message):
        return

    config = get_config(context)
    db_pool = get_db(context)
    if not await auth_check(update, config, db_pool):
        return

    # Delegate to review_start by patching the update so it carries a message.
    # review_start only uses update.message.reply_text and update.effective_chat
    # for auth — both are accessible via the callback query's message.
    update.message = query.message  # type: ignore[assignment]
    await review_start(update, context)


@rate_limit(max_calls=10, window_seconds=60)
async def task_done_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``task_done_{id}`` — mark a task as complete via ProjectManager."""
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    if not isinstance(query.message, Message):
        return

    config = get_config(context)
    db_pool = get_db(context)
    if not await auth_check(update, config, db_pool):
        return

    if not query.data or not (match := re.search(r"task_done_(\d+)", query.data)):
        return
    task_id = int(match.group(1))

    db = get_db(context)
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


@rate_limit(max_calls=20, window_seconds=60)
async def pulse_rating_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``pulse_{up,down,save}_{id}`` — record a Pulse card rating.

    POSTs to ``/api/pulse/rate`` on paper_ingestion and answers the callback
    query with a short confirmation (or an error on failure).
    """
    query = update.callback_query
    if query is None:
        return
    config = get_config(context)
    db_pool = get_db(context)
    if not await auth_check(update, config, db_pool):
        await query.answer()
        return

    match = re.fullmatch(r"pulse_(up|down|save)_(\d+)", query.data or "")
    if not match:
        await query.answer(text="Invalid rating")
        return
    rating = match.group(1)
    paper_id = int(match.group(2))

    http = get_http(context)
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
    """Register all callback query handlers on the given application."""
    app.add_handler(CallbackQueryHandler(paper_detail_callback, pattern=r"^paper_detail_\d+$"))
    app.add_handler(CallbackQueryHandler(paper_bookmark_callback, pattern=r"^paper_bookmark_\d+$"))
    app.add_handler(CallbackQueryHandler(project_detail_callback, pattern=r"^project_detail_\d+$"))
    app.add_handler(CallbackQueryHandler(task_done_callback, pattern=r"^task_done_\d+$"))
    app.add_handler(CallbackQueryHandler(start_review_callback, pattern=r"^start_review$"))
    app.add_handler(
        CallbackQueryHandler(pulse_rating_callback, pattern=r"^pulse_(up|down|save)_\d+$")
    )
