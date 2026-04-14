"""Command handlers for the JARVIS Telegram bot.

Implements all slash-command interactions: /start, /help, /papers, /stats,
/briefing, /projects, /tasks, /done, and /newproject.
"""

from __future__ import annotations

import json
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
from app.handlers.rate_limit import rate_limit
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
        db_pool = _get_db(context)
        if not await _auth_check(update, config, db_pool):
            chat_id = update.effective_chat.id if update.effective_chat else "unknown"
            logger.warning(
                "Unauthorised access attempt from chat_id=%s",
                chat_id,
            )
            return
        return await func(update, context)

    return wrapper


async def _handle_pairing(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    code: str,
) -> None:
    """Complete the dashboard-initiated Telegram pairing flow.

    Looks up ``telegram_pairing`` for the given ``code`` under a row lock.
    If the code exists and has not expired, persists the current chat id into
    ``user_config.telegram.owner_chat_id`` (as a JSON integer) and deletes the
    used code. Invalid/expired codes are also cleaned up opportunistically.

    Rate-limited to 5 attempts per 60 s per chat to prevent brute-forcing.
    """
    import hashlib
    import time

    from app.handlers.rate_limit import _timestamps

    db_pool = _get_db(context)
    chat = update.effective_chat
    message = update.message
    if chat is None or message is None:
        return

    # --- inline rate-limit: 5 pairing attempts per 60 s per chat ---
    _rl_key = f"{chat.id}:_handle_pairing"
    _now = time.monotonic()
    _window = 60
    _max = 5
    _stamps = _timestamps[_rl_key]
    _stamps[:] = [t for t in _stamps if _now - t < _window]
    if len(_stamps) >= _max:
        logger.warning("pairing rate-limited chat_id=%s", chat.id)
        await message.reply_text(
            f"Too many pairing attempts — please wait {_window}s before trying again."
        )
        return
    _stamps.append(_now)

    code_hash = hashlib.sha256(code.encode()).hexdigest()[:8]
    try:
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                # Refuse if an owner is already paired (takeover prevention)
                current_owner = await conn.fetchval(
                    "SELECT value FROM user_config WHERE key = 'telegram.owner_chat_id'"
                )
                if current_owner not in (None, "null"):
                    await message.reply_text(
                        "This JARVIS instance is already paired. "
                        "Unpair from the dashboard first (Settings → Integrations)."
                    )
                    logger.info("pairing refused: owner already set, code_hash=%s", code_hash)
                    return
                row = await conn.fetchrow(
                    "SELECT expires_at FROM telegram_pairing WHERE code = $1 FOR UPDATE",
                    code,
                )
                if row is None:
                    await message.reply_text("Invalid or expired pairing code.")
                    return
                if row["expires_at"] < datetime.now(UTC):
                    await conn.execute("DELETE FROM telegram_pairing WHERE code = $1", code)
                    await message.reply_text("Invalid or expired pairing code.")
                    return
                await conn.execute(
                    "UPDATE user_config SET value = $1::jsonb, updated_at = NOW() "
                    "WHERE key = 'telegram.owner_chat_id'",
                    json.dumps(chat.id),
                )
                await conn.execute("DELETE FROM telegram_pairing WHERE code = $1", code)
        await message.reply_text("✅ Paired! You'll now receive JARVIS notifications here.")
    except Exception:
        logger.exception("pairing_failed code_hash=%s", code_hash)  # hash only — not raw code
        await message.reply_text("Pairing failed — please try again from the dashboard.")


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


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/start`` — pairing deep-link or welcome message.

    A Telegram deep-link of the form ``/start PAIR_<code>`` completes the
    dashboard-initiated pairing flow (sets ``user_config.telegram.owner_chat_id``
    to this chat's id) without requiring a pre-configured ``TELEGRAM_CHAT_ID``.
    This is the ONLY un-authed bot entrypoint; all other ``/start`` invocations
    still go through :func:`_auth_check` before replying.

    Parameters
    ----------
    update : Update
        Incoming Telegram update.
    context : ContextTypes.DEFAULT_TYPE
        Bot context.
    """
    message = update.message
    raw_text = getattr(message, "text", None) if message is not None else None
    if isinstance(raw_text, str):
        parts = raw_text.split(maxsplit=1)
        if len(parts) > 1 and parts[1].startswith("PAIR_"):
            await _handle_pairing(update, context, parts[1][len("PAIR_") :])
            return

    config = _get_config(context)
    db_pool = _get_db(context)
    if not await _auth_check(update, config, db_pool):
        chat_id = update.effective_chat.id if update.effective_chat else "unknown"
        logger.warning("Unauthorised /start attempt from chat_id=%s", chat_id)
        return

    if update.message is None:
        return
    text = (
        "Welcome to <b>JARVIS RD Assistant</b>!\n\n"
        "I help you manage research papers, flashcard reviews, and projects.\n\n" + format_help()
    )
    await update.message.reply_text(text, parse_mode="HTML")


@auth_required
@rate_limit(max_calls=5, window_seconds=60)
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/help`` — display available commands.

    Parameters
    ----------
    update : Update
        Incoming Telegram update.
    context : ContextTypes.DEFAULT_TYPE
        Bot context.
    """
    if update.message is None:
        return
    await update.message.reply_text(format_help(), parse_mode="HTML")


@auth_required
@rate_limit(max_calls=5, window_seconds=60)
async def papers_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/papers [query]`` — search or list recent papers.

    Parameters
    ----------
    update : Update
        Incoming Telegram update.
    context : ContextTypes.DEFAULT_TYPE
        Bot context.
    """
    if update.message is None:
        return
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
    """Handle ``/stats`` — show learning statistics.

    Parameters
    ----------
    update : Update
        Incoming Telegram update.
    context : ContextTypes.DEFAULT_TYPE
        Bot context.
    """
    if update.message is None:
        return
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
        await update.message.reply_text("Failed to retrieve learning stats.", parse_mode="HTML")
        return

    await update.message.reply_text(format_review_stats(stats), parse_mode="HTML")


@auth_required
@rate_limit(max_calls=3, window_seconds=60)
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
    if update.message is None:
        return
    db = _get_db(context)
    http = _get_http(context)
    config = _get_config(context)

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
async def projects_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/projects`` — list active projects.

    Parameters
    ----------
    update : Update
        Incoming Telegram update.
    context : ContextTypes.DEFAULT_TYPE
        Bot context.
    """
    if update.message is None:
        return
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
@rate_limit(max_calls=5, window_seconds=60)
async def tasks_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/tasks [project_id]`` — list in-progress tasks.

    Parameters
    ----------
    update : Update
        Incoming Telegram update.
    context : ContextTypes.DEFAULT_TYPE
        Bot context.
    """
    if update.message is None:
        return
    db = _get_db(context)
    project_id = None
    if context.args:
        try:
            project_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("Usage: /tasks [project_id]", parse_mode="HTML")
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

    await update.message.reply_text(truncate("\n".join(lines)), parse_mode="HTML")


@auth_required
@rate_limit(max_calls=10, window_seconds=60)
async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/done <task_id>`` — mark a task as complete.

    Parameters
    ----------
    update : Update
        Incoming Telegram update.
    context : ContextTypes.DEFAULT_TYPE
        Bot context.
    """
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
    """Handle ``/newproject <name>`` — create a new project.

    Parameters
    ----------
    update : Update
        Incoming Telegram update.
    context : ContextTypes.DEFAULT_TYPE
        Bot context.
    """
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


_MAX_FOCUS_MINUTES = 480  # 8 hours — prevents resource exhaustion


@auth_required
@rate_limit(max_calls=3, window_seconds=60)
async def focus_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/focus [duration]`` — start a focus session."""
    if update.message is None or update.effective_chat is None:
        return
    if context.job_queue is None:
        await update.message.reply_text(
            "Focus sessions are unavailable (job queue not initialised).",
            parse_mode="HTML",
        )
        return
    args = context.args or []
    try:
        minutes = min(int(args[0]) if args else 25, _MAX_FOCUS_MINUTES)
    except ValueError:
        await update.message.reply_text(
            "Please provide a valid integer for duration.",
            parse_mode="HTML",  # noqa: E501
        )
        return

    chat_id = update.effective_chat.id

    async def focus_alarm(context: ContextTypes.DEFAULT_TYPE) -> None:
        job = context.job
        if job is None or job.chat_id is None:
            return
        data_minutes = job.data if isinstance(job.data, int | float) else 0
        await context.bot.send_message(
            job.chat_id,
            text=f"🍅 Focus session complete ({data_minutes} minutes). Did you finish your task? Want to add any notes?",  # noqa: E501,
        )
        try:
            http = _get_http(context)
            config = _get_config(context)
            await http.post(
                f"{config.learning_engine_url}/api/executive/focus/log",
                json={"duration_hours": data_minutes / 60},
                timeout=10.0,
            )
        except Exception:
            logger.exception("Failed to log focus session to backend")

    # Cancel any existing focus timer for this chat
    for job in context.job_queue.get_jobs_by_name(f"focus_{chat_id}"):
        job.schedule_removal()

    context.job_queue.run_once(
        focus_alarm, minutes * 60, chat_id=chat_id, name=f"focus_{chat_id}", data=minutes
    )
    await update.message.reply_text(
        f"🍅 Focus session started for {minutes} minutes. Notifications are paused.",
        parse_mode="HTML",
    )


@auth_required
@rate_limit(max_calls=1, window_seconds=60, cooldown_seconds=300)
async def pulse_now_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/pulse_now`` — trigger immediate Pulse generation.

    Parameters
    ----------
    update : Update
        Incoming Telegram update.
    context : ContextTypes.DEFAULT_TYPE
        Bot context.
    """
    if update.message is None:
        return
    http = _get_http(context)
    config = _get_config(context)
    headers = {}
    if config.jarvis_api_key:
        headers["X-API-Key"] = config.jarvis_api_key
    try:
        resp = await http.post(
            f"{config.paper_ingestion_url}/api/pulse/generate",
            headers=headers,
            timeout=15.0,
        )
        resp.raise_for_status()
    except Exception:
        logger.exception("Failed to trigger Pulse generation")
        await update.message.reply_text(
            "Failed to trigger Pulse generation. Try again later.",
            parse_mode="HTML",
        )
        return

    await update.message.reply_text(
        "⚡ Pulse generation started. Check back in a few minutes.",
        parse_mode="HTML",
    )


@auth_required
@rate_limit(max_calls=5, window_seconds=60)
async def next_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/next`` — recommend the next paper to read."""
    if update.message is None:
        return
    db = _get_db(context)
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
    app.add_handler(CommandHandler("focus", focus_command))
    app.add_handler(CommandHandler("next", next_command))
    app.add_handler(CommandHandler("pulse_now", pulse_now_command))
