"""Inline-keyboard callback handlers for the JARVIS Telegram bot.

Handles non-review callbacks triggered by inline keyboard buttons on
paper listings, project listings, and task actions.

Spec §5.3 callback name convention:
    paper:<action>:<id>                              — lifecycle / curation
    paper:feedback_(pos|neg):<id>:<source>           — per-paper feedback signal

H1 invariant: each execution path performs exactly ONE ``query.answer()``.
Bare early answers on rejection paths are H1-compliant (single answer per
path).  Never call ``query.answer()`` twice on a single execution path.
"""

from __future__ import annotations

import logging
import re

from telegram import Message, Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

from telegram_bot import services_client
from telegram_bot.formatters import format_paper_detail, format_project_status
from telegram_bot.handlers.helpers import _owner_headers, auth_check, get_config, get_db, get_http
from telegram_bot.handlers.rate_limit import rate_limit

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
# Shared auth preamble
# ---------------------------------------------------------------------------


async def _callback_auth(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> tuple[bool, int | None]:
    """Run the shared callback auth check and return ``(authorized, jarvis_user_id)``.

    Callers MUST call ``query.answer()`` (H1) themselves when this returns
    ``(False, None)``; this helper deliberately does NOT call ``query.answer()``
    so that callers can provide a custom message if needed.
    """
    config = get_config(context)
    db_pool = get_db(context)
    return await auth_check(update, config, db_pool)


# ---------------------------------------------------------------------------
# Callback handlers
# ---------------------------------------------------------------------------


@rate_limit(max_calls=10, window_seconds=60)
async def paper_detail_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``paper_detail_{id}`` — fetch paper from paper_ingestion API and show detail."""
    query = update.callback_query
    if query is None:
        return
    if not isinstance(query.message, Message):
        await query.answer()
        return

    authorized, jarvis_user_id = await _callback_auth(update, context)
    if not authorized:
        await query.answer()  # H1: ack even on auth failure so Telegram stops the spinner
        return

    await query.answer()

    if not query.data or not (match := re.search(r"paper_detail_(\d+)", query.data)):
        return
    paper_id = int(match.group(1))

    config = get_config(context)
    http = get_http(context)
    try:
        resp = await http.get(
            f"{config.paper_ingestion_url}/api/papers/{paper_id}",
            headers=_owner_headers(config, jarvis_user_id),
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
    Preserves the H1 invariant (single ``query.answer()`` per success path).
    """
    query = update.callback_query
    if query is None:
        return
    if not isinstance(query.message, Message):
        await query.answer()
        return

    authorized, jarvis_user_id = await _callback_auth(update, context)
    if not authorized:
        await query.answer()  # H1: every path answers exactly once
        return

    if not query.data or not (m := _PAPER_ACTION_RE.match(query.data)):
        await query.answer()
        return
    action = m.group("action")
    paper_id = int(m.group("id"))

    method, suffix = _PAPER_ACTION_ENDPOINTS[action]
    label = _PAPER_ACTION_LABELS[action]

    config = get_config(context)
    http = get_http(context)
    try:
        resp = await http.request(
            method,
            f"{config.paper_ingestion_url}/api/papers/{paper_id}/{suffix}",
            headers=_owner_headers(config, jarvis_user_id),
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

    POSTs to ``/api/papers/{id}/feedback``.  Preserves the H1 invariant
    (single ``query.answer()`` per success path).
    """
    query = update.callback_query
    if query is None:
        return
    if not isinstance(query.message, Message):
        await query.answer()
        return

    authorized, jarvis_user_id = await _callback_auth(update, context)
    if not authorized:
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

    config = get_config(context)
    http = get_http(context)
    try:
        resp = await http.post(
            f"{config.paper_ingestion_url}/api/papers/{paper_id}/feedback",
            json={"signal": signal, "source": source},
            headers=_owner_headers(config, jarvis_user_id),
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
    if not isinstance(query.message, Message):
        await query.answer()
        return

    authorized, jarvis_user_id = await _callback_auth(update, context)
    if not authorized:
        await query.answer()  # H1: ack even on auth failure so Telegram stops the spinner
        return
    assert jarvis_user_id is not None  # noqa: S101 — guaranteed by auth_check invariant

    await query.answer()

    if not query.data or not (match := re.search(r"project_detail_(\d+)", query.data)):
        return
    project_id = int(match.group(1))

    config = get_config(context)
    http = get_http(context)
    try:
        project = await services_client.fetch_project(http, config, jarvis_user_id, project_id)
        if project is None:
            await query.message.reply_text("Project not found.", parse_mode="HTML")
            return
        tasks = await services_client.fetch_project_tasks(http, config, jarvis_user_id, project_id)
        milestones = await services_client.fetch_project_milestones(
            http, config, jarvis_user_id, project_id
        )
    except Exception:
        logger.exception("Failed to load project detail for id=%s", project_id)
        await query.message.reply_text("⚠️ Couldn't load that right now.", parse_mode="HTML")
        return

    text = format_project_status(project, tasks, milestones)
    await query.message.reply_text(text, parse_mode="HTML")


@rate_limit(max_calls=10, window_seconds=60)
async def task_done_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``task_done_{id}`` — mark a task done via the Learning Engine REST API.

    Ownership is enforced server-side: the LE ``PUT /api/tasks/{id}`` endpoint scopes
    by the forwarded ``X-Owner-User-Id`` header, so a non-owned task returns 404 →
    "not found" with no existence leak (TG-SEC-03 now lives in the LE contract).
    """
    query = update.callback_query
    if query is None:
        return
    if not isinstance(query.message, Message):
        await query.answer()
        return

    authorized, jarvis_user_id = await _callback_auth(update, context)
    if not authorized:
        await query.answer()  # H1: ack even on auth failure so Telegram stops the spinner
        return
    assert jarvis_user_id is not None  # noqa: S101 — guaranteed by auth_check invariant
    if context.user_data is not None:
        context.user_data["jarvis_user_id"] = jarvis_user_id

    await query.answer()

    if not query.data or not (match := re.search(r"task_done_(\d+)", query.data)):
        return
    task_id = int(match.group(1))

    config = get_config(context)
    http = get_http(context)
    try:
        result = await services_client.complete_task(http, config, jarvis_user_id, task_id)
    except Exception:
        logger.exception("Failed to complete task id=%s", task_id)
        await query.message.reply_text("⚠️ Couldn't load that right now.", parse_mode="HTML")
        return

    if result is None:
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
    # TG-003: start_review is intentionally NOT registered here.
    # review_handler.ConversationHandler owns the /review flow; a duplicate
    # registration would cause dual-dispatch.
