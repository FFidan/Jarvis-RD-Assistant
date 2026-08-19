"""System command handlers: /start, /help, /focus."""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable

import httpx
from telegram import Update
from telegram.ext import ContextTypes

from telegram_bot import platform_client, services_client
from telegram_bot.config import BotConfig
from telegram_bot.focus_contract import FocusSession, FocusTransition
from telegram_bot.formatters import format_help
from telegram_bot.handlers.commands._auth import auth_required
from telegram_bot.handlers.helpers import (
    auth_check,
    get_config,
    get_http,
    get_jarvis_user_id,
    get_platform_http,
)
from telegram_bot.handlers.rate_limit import rate_limit
from telegram_bot.orchestration.research_pulse import deliver_pulse_to_chat
from telegram_bot.pulse_contract import PulseGenerateStatus

logger = logging.getLogger(__name__)

_MAX_FOCUS_MINUTES = 480  # 8 hours — prevents resource exhaustion

#: Focus length used when the user's saved preference cannot be read. It is the
#: web app's own default, and the reply always says the substitution happened.
_FALLBACK_FOCUS_MINUTES = 25

#: Sub-commands that act on an already-open focus interval.
_FOCUS_TRANSITIONS = frozenset({"pause", "resume", "stop"})

#: One-line reminder of everything /focus accepts.
_FOCUS_USAGE = (
    "Usage: <code>/focus</code> for status, <code>/focus start [minutes]</code>, "
    "<code>/focus pause</code>, <code>/focus resume</code>, <code>/focus stop</code>."
)

#: Seconds between two Pulse job-status reads while ``/pulse_now`` waits.
_PULSE_POLL_INTERVAL_SECONDS = 10.0

#: Total seconds ``/pulse_now`` waits for a deck before telling the user it is
#: still running. A full Pulse run is LLM-bound, so the wait is bounded rather
#: than open-ended: the handler always answers, and the deck stays reachable
#: through /next and the web deck afterwards.
_PULSE_POLL_BUDGET_SECONDS = 180.0


@rate_limit(max_calls=10, window_seconds=60)
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/start`` — welcome message (auth-gated by pairing).

    ``/start`` goes through :func:`auth_check`. An unpaired chat is shown the
    ``/pair`` guidance; a paired chat receives the welcome message. Pairing is
    the sole bot-identity mechanism (see :func:`auth_check`).

    Parameters
    ----------
    update : Update
        Incoming Telegram update.
    context : ContextTypes.DEFAULT_TYPE
        Bot context.
    """
    config = get_config(context)
    platform_http = get_platform_http(context)
    authorized, jarvis_user_id = await auth_check(update, config, platform_http)
    if not authorized:
        chat_id = update.effective_chat.id if update.effective_chat else "unknown"
        logger.warning("Unauthorised /start attempt from chat_id=%s", chat_id)
        if update.message is not None:
            await update.message.reply_text(
                "🔗 Link your JARVIS account first: open the dashboard → "
                "Settings → Integrations → Telegram, then run /pair <code>."
            )
        return
    if context.user_data is not None:
        context.user_data["jarvis_user_id"] = jarvis_user_id

    if update.message is None:
        return
    text = (
        "Welcome to <b>JARVIS RD Assistant</b>!\n\n"
        "I help you manage research papers, flashcard reviews, and projects.\n\n" + format_help()
    )
    await update.message.reply_text(text, parse_mode="HTML")


@rate_limit(max_calls=5, window_seconds=60)
@auth_required
async def help_command(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/help`` — display available commands.

    Parameters
    ----------
    update : Update
        Incoming Telegram update.
    _context : ContextTypes.DEFAULT_TYPE
        Bot context (unused — ``/help`` only formats static text).
    """
    if update.message is None:
        return
    await update.message.reply_text(format_help(), parse_mode="HTML")


@rate_limit(max_calls=1, window_seconds=60, cooldown_seconds=300)
@auth_required
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
    http = get_http(context)
    config = get_config(context)
    jarvis_user_id = get_jarvis_user_id(context)
    assert jarvis_user_id is not None  # noqa: S101 — guaranteed by @auth_required
    try:
        job = await services_client.trigger_pulse_generation(http, config, jarvis_user_id)
    except Exception:
        logger.exception("Failed to trigger Pulse generation")
        await update.message.reply_text(
            "Failed to trigger Pulse generation. Try again later.",
            parse_mode="HTML",
        )
        return

    await update.message.reply_text(
        "⚡ Pulse generation started. I'll send the deck here when it's ready.",
        parse_mode="HTML",
    )

    status = await _await_pulse_job(http, config, jarvis_user_id, job.job_id)
    if status is None:
        await update.message.reply_text(
            "Pulse is still generating. Run /next in a few minutes to read the new deck.",
            parse_mode="HTML",
        )
        return
    if status.status != "succeeded":
        await update.message.reply_text(
            "Pulse generation did not finish. Try /pulse_now again later.",
            parse_mode="HTML",
        )
        return

    chat = update.effective_chat
    if chat is None:
        return
    await deliver_pulse_to_chat(http, context.bot, config, chat.id, jarvis_user_id)


async def _await_pulse_job(
    http: httpx.AsyncClient,
    config: BotConfig,
    user_id: int,
    job_id: str,
) -> PulseGenerateStatus | None:
    """Wait for one Pulse generation job within a fixed budget.

    Parameters
    ----------
    http : httpx.AsyncClient
        Client carrying the paired-user marker.
    config : BotConfig
        Bot configuration.
    user_id : int
        Paired JARVIS user the job belongs to.
    job_id : str
        Identifier the generation request returned.

    Returns
    -------
    PulseGenerateStatus or None
        The terminal status, or ``None`` when the budget ran out first or the
        status could not be read. A transient read failure is retried until the
        budget expires rather than reported as a finished job.
    """
    deadline = asyncio.get_running_loop().time() + _PULSE_POLL_BUDGET_SECONDS
    while True:
        try:
            status = await services_client.fetch_pulse_generation_status(
                http, config, user_id, job_id
            )
        except Exception:
            logger.exception("Failed to read Pulse generation status")
            status = None
        if status is not None and status.is_terminal:
            return status
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return None
        await asyncio.sleep(min(_PULSE_POLL_INTERVAL_SECONDS, remaining))


@rate_limit(max_calls=3, window_seconds=60)
@auth_required
async def focus_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/focus`` — report the focus timer, or start, pause, resume, or stop it.

    With no arguments the command reports state rather than starting anything,
    so an already-running session is visible before a second start is attempted.
    """
    if update.message is None or update.effective_chat is None:
        return
    reply = update.message.reply_text
    args = context.args or []
    jarvis_user_id = get_jarvis_user_id(context)
    assert jarvis_user_id is not None  # noqa: S101 -- guaranteed by @auth_required

    action = args[0].lower() if args else "status"
    if action == "status":
        await _reply_focus_status(reply, context, jarvis_user_id)
        return
    if action in _FOCUS_TRANSITIONS:
        await _apply_focus_transition(reply, context, jarvis_user_id, action)
        return

    if action == "start":
        raw_minutes = args[1] if len(args) > 1 else None
    else:
        raw_minutes = args[0]
    if raw_minutes is None:
        await start_focus_and_reply(reply, context, jarvis_user_id, None)
        return
    try:
        minutes = int(raw_minutes)
    except ValueError:
        await reply(_FOCUS_USAGE, parse_mode="HTML")
        return
    if minutes <= 0:
        await reply(f"Duration must be at least 1 minute. {_FOCUS_USAGE}", parse_mode="HTML")
        return
    await start_focus_and_reply(reply, context, jarvis_user_id, minutes)


async def start_focus_and_reply(
    reply: Callable[..., Awaitable[object]],
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    minutes: int | None,
) -> None:
    """Start one focus interval and report the outcome through *reply*.

    Parameters
    ----------
    reply : callable
        Coroutine function that sends one message, e.g. ``reply_text``.
    context : ContextTypes.DEFAULT_TYPE
        Bot context carrying the backend and Platform clients.
    user_id : int
        Paired JARVIS user starting the session.
    minutes : int or None
        Explicit length, or ``None`` to use the length the user saved in the
        web app. When that preference cannot be read the standard length is
        used instead and the reply says so, rather than silently substituting
        a number the user never chose.
    """
    notice = ""
    if minutes is None:
        preferences = await fetch_timer_preferences(context, user_id)
        if preferences is None:
            minutes = _FALLBACK_FOCUS_MINUTES
            notice = (
                " Your saved focus length was unreachable, so this used the standard "
                f"{_FALLBACK_FOCUS_MINUTES} minutes."
            )
        else:
            minutes = preferences.work_minutes
    minutes = min(minutes, _MAX_FOCUS_MINUTES)

    try:
        await services_client.start_focus_session(
            get_http(context),
            get_config(context),
            user_id,
            minutes * 60,
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 409:
            await reply(
                "A focus session is already active. Run <code>/focus</code> to see it, "
                "or <code>/focus stop</code> to end it.",
                parse_mode="HTML",
            )
            return
        logger.exception("Failed to start focus session")
        await reply("The focus session could not be started. Try again later.", parse_mode="HTML")
        return
    except Exception:
        logger.exception("Failed to start focus session")
        await reply("The focus session could not be started. Try again later.", parse_mode="HTML")
        return

    await reply(
        f"Focus session started for {minutes} minutes. Scheduled notifications are paused.{notice}",
        parse_mode="HTML",
    )


async def fetch_timer_preferences(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
) -> platform_client.TimerPreferences | None:
    """Return the user's saved timer preference, or ``None`` when it is unreadable.

    Parameters
    ----------
    context : ContextTypes.DEFAULT_TYPE
        Bot context carrying the Platform client.
    user_id : int
        Paired JARVIS user whose preference is read.

    Returns
    -------
    platform_client.TimerPreferences or None
        The saved preference, or ``None`` so callers can say what they had to
        substitute instead of presenting a guess as the user's setting.
    """
    try:
        return await platform_client.get_timer_preferences(
            get_platform_http(context),
            get_config(context),
            user_id,
        )
    except (httpx.HTTPError, RuntimeError):
        logger.warning("Timer preference lookup failed", exc_info=True)
        return None


async def _reply_focus_status(
    reply: Callable[..., Awaitable[object]],
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
) -> None:
    """Report timer state, the day's total against the target, and the streak."""
    http = get_http(context)
    config = get_config(context)
    try:
        session = await services_client.fetch_active_focus_session(http, config, user_id)
    except Exception:
        logger.exception("Failed to read the focus session")
        await reply("The focus timer could not be read. Try again later.", parse_mode="HTML")
        return

    lines = [_focus_state_line(session)]
    preferences = await fetch_timer_preferences(context, user_id)
    try:
        summary = await services_client.fetch_my_day_focus(http, config, user_id)
    except Exception:
        logger.warning("Failed to read today's focus totals", exc_info=True)
        summary = None

    if summary is None:
        lines.append("Today's focus total is unavailable right now.")
    else:
        today_minutes = round(summary.today_focus_hours * 60)
        if preferences is None:
            lines.append(f"Today: {today_minutes} min focused; your daily target was unreachable.")
        else:
            target_minutes = preferences.work_minutes * preferences.target_cycles
            lines.append(f"Today: {today_minutes} of {target_minutes} target minutes.")
        days = summary.focus_streak_days
        lines.append(f"Streak: {days} day{'' if days == 1 else 's'}.")
    lines.append(_FOCUS_USAGE)
    await reply("\n".join(lines), parse_mode="HTML")


async def _apply_focus_transition(
    reply: Callable[..., Awaitable[object]],
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    action: str,
) -> None:
    """Pause, resume, or stop the user's open focus interval."""
    http = get_http(context)
    config = get_config(context)
    try:
        session = await services_client.fetch_active_focus_session(http, config, user_id)
    except Exception:
        logger.exception("Failed to read the focus session")
        await reply("The focus timer could not be read. Try again later.", parse_mode="HTML")
        return
    if session is None or session.state == "completed":
        await reply(
            "No focus session is running. Start one with <code>/focus start</code>.",
            parse_mode="HTML",
        )
        return

    try:
        if action == "pause":
            transition = await services_client.pause_focus_session(
                http, config, user_id, session.id
            )
        elif action == "resume":
            transition = await services_client.resume_focus_session(
                http, config, user_id, session.id
            )
        else:
            transition = await services_client.complete_focus_session(
                http, config, user_id, session.id, "stop"
            )
    except Exception:
        logger.exception("Failed to apply focus transition %s", action)
        await reply("That focus change did not go through. Try again later.", parse_mode="HTML")
        return

    await reply(_focus_transition_message(action, transition), parse_mode="HTML")


def _focus_state_line(session: FocusSession | None) -> str:
    """Describe the timer's current state in one sentence."""
    if session is None or session.state == "completed":
        return "No focus session is running."
    remaining = math.ceil(session.remaining_seconds / 60)
    if session.state == "paused":
        return f"Focus paused — {remaining} min left."
    return f"Focus running — {remaining} min left."


def _focus_transition_message(action: str, transition: FocusTransition) -> str:
    """Report what a transition actually did, including when it changed nothing."""
    remaining = math.ceil(transition.session.remaining_seconds / 60)
    if action == "pause":
        if not transition.changed:
            return "That focus session was already paused."
        return f"Focus paused — {remaining} min left. Resume with <code>/focus resume</code>."
    if action == "resume":
        if not transition.changed:
            return "That focus session was already running."
        return f"Focus resumed — {remaining} min left."
    if not transition.changed:
        return "That focus session was already finished."
    recorded = round(transition.session.recorded_seconds / 60)
    return f"Focus stopped — {recorded} minutes recorded."
