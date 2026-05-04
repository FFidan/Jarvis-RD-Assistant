"""Inline-keyboard callback handlers for the JARVIS Telegram bot.

Handles non-review callbacks triggered by inline keyboard buttons on
paper listings, project listings, and task actions.

Spec §5.3 callback name convention:
    paper:<action>:<id>                              — lifecycle / curation
    paper:feedback_(pos|neg):<id>:<source>           — per-paper feedback signal

WS-AH2 H1 invariant: each execution path performs exactly ONE
``query.answer()``.  Bare early answers on rejection paths are H1-compliant
(single answer per path).  Never call ``query.answer()`` twice on a single
execution path.
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
# Dispatch tables (spec §5.3)
# ---------------------------------------------------------------------------


_PAPER_ACTION_ENDPOINTS: dict[str, tuple[str, str]] = {
    "save": ("PUT", "save"),
    "skip": ("PUT", "skip"),
    "reading": ("PUT", "reading"),
    "done": ("PUT", "done"),
    "trash": ("PUT", "trash"),
    "restore": ("PUT", "restore"),
    "trash_reject": ("PUT", "trash_and_reject"),
    "star": ("PUT", "star"),
    "unstar": ("PUT", "unstar"),
}

_PAPER_ACTION_LABELS: dict[str, str] = {
    "save": "💾 Saved",
    "skip": "⏩ Skipped",
    "reading": "📖 Marked Reading",
    "done": "✓ Marked Done",
    "trash": "🗑 Trashed",
    "restore": "↩ Restored",
    "trash_reject": "🗑+👎 Trashed & Rejected",
    "star": "⭐ Starred",
    "unstar": "☆ Unstarred",
}

_PAPER_ACTION_RE = re.compile(
    r"^paper:(?P<action>save|skip|reading|done|trash|restore|trash_reject|star|unstar):(?P<id>\d+)$"
)

_PAPER_FEEDBACK_RE = re.compile(
    r"^paper:feedback_(?P<sign>pos|neg):(?P<id>\d+):(?P<source>pulse_thumbs|feed_thumbs|paper_detail_thumbs|dismiss_combined)$"
)


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
    headers: dict[str, str] = {}
    if config.jarvis_api_key:
        headers["X-API-Key"] = config.jarvis_api_key.get_secret_value()
    try:
        resp = await http.get(
            f"{config.paper_ingestion_url}/api/papers/{paper_id}",
            headers=headers,
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
async def paper_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``paper:<action>:<id>`` — lifecycle / curation via backend API.

    Spec §5.3 callback convention.  Dispatches via :data:`_PAPER_ACTION_ENDPOINTS`.
    Preserves WS-AH2 H1 invariant (single ``query.answer()`` per success path).
    """
    query = update.callback_query
    if query is None:
        return
    if not isinstance(query.message, Message):
        await query.answer()
        return

    config = get_config(context)
    db_pool = get_db(context)
    if not await auth_check(update, config, db_pool):
        await query.answer()  # H1: every path answers exactly once
        return

    if not query.data or not (m := _PAPER_ACTION_RE.match(query.data)):
        await query.answer()
        return
    action = m.group("action")
    paper_id = int(m.group("id"))

    method, suffix = _PAPER_ACTION_ENDPOINTS[action]
    label = _PAPER_ACTION_LABELS[action]

    http = get_http(context)
    headers: dict[str, str] = {}
    if config.jarvis_api_key:
        headers["X-API-Key"] = config.jarvis_api_key.get_secret_value()
    try:
        resp = await http.request(
            method,
            f"{config.paper_ingestion_url}/api/papers/{paper_id}/{suffix}",
            headers=headers,
            timeout=15.0,
        )
        resp.raise_for_status()
        await query.answer(text=label)
        await query.message.reply_text(f"{label} <b>paper {paper_id}</b>.", parse_mode="HTML")
    except Exception:
        logger.exception("Failed to %s paper id=%s", action, paper_id)
        await query.answer(text=f"{action} failed — try again later")


@rate_limit(max_calls=10, window_seconds=60)
async def paper_feedback_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``paper:feedback_(pos|neg):<id>:<source>`` — record per-paper feedback.

    POSTs to ``/api/papers/{id}/feedback`` (Wave 1cd endpoint).  Preserves
    WS-AH2 H1 invariant (single ``query.answer()`` per success path).
    """
    query = update.callback_query
    if query is None:
        return
    if not isinstance(query.message, Message):
        await query.answer()
        return

    config = get_config(context)
    db_pool = get_db(context)
    if not await auth_check(update, config, db_pool):
        await query.answer()  # H1: every path answers exactly once
        return

    if not query.data or not (m := _PAPER_FEEDBACK_RE.match(query.data)):
        await query.answer()
        return
    sign = m.group("sign")
    paper_id = int(m.group("id"))
    source = m.group("source")
    signal = "positive" if sign == "pos" else "negative"
    label = "👍 Recorded" if sign == "pos" else "👎 Recorded"

    http = get_http(context)
    headers: dict[str, str] = {}
    if config.jarvis_api_key:
        headers["X-API-Key"] = config.jarvis_api_key.get_secret_value()
    try:
        resp = await http.post(
            f"{config.paper_ingestion_url}/api/papers/{paper_id}/feedback",
            json={"signal": signal, "source": source},
            headers=headers,
            timeout=15.0,
        )
        resp.raise_for_status()
        await query.answer(text=label)
    except Exception:
        logger.exception("Failed to record feedback for paper id=%s signal=%s", paper_id, signal)
        await query.answer(text="Feedback failed — try again later")


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

    # Guard against InaccessibleMessage — can arrive when the message is older
    # than 48 hours.  A bare assignment silently casts the wrong type; instead
    # we answer with an alert so the user gets feedback.
    if not isinstance(query.message, Message):
        await query.answer("This message is no longer accessible", show_alert=True)
        return

    config = get_config(context)
    db_pool = get_db(context)
    if not await auth_check(update, config, db_pool):
        return

    # Delegate to review_start, passing the callback message explicitly so that
    # update.message is never mutated (Update fields are conceptually immutable
    # per call and the assignment was fragile / type-unsafe).
    await review_start(update, context, message=query.message)


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


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_callback_handlers(app: Application) -> None:
    """Register paper / project / task callbacks (TG-003: review owned by ConversationHandler)."""
    app.add_handler(CallbackQueryHandler(paper_detail_callback, pattern=r"^paper_detail_\d+$"))
    app.add_handler(
        CallbackQueryHandler(
            paper_action_callback,
            pattern=r"^paper:(save|skip|reading|done|trash|restore|trash_reject|star|unstar):\d+$",
        )
    )
    app.add_handler(
        CallbackQueryHandler(
            paper_feedback_callback,
            pattern=r"^paper:feedback_(pos|neg):\d+:(pulse_thumbs|feed_thumbs|paper_detail_thumbs|dismiss_combined)$",
        )
    )
    app.add_handler(CallbackQueryHandler(project_detail_callback, pattern=r"^project_detail_\d+$"))
    app.add_handler(CallbackQueryHandler(task_done_callback, pattern=r"^task_done_\d+$"))
    # start_review_callback intentionally NOT registered here — review_handler.py
    # owns the ConversationHandler (TG-003).
