"""Command handler registration for the JARVIS Telegram bot."""

from __future__ import annotations

from telegram.ext import Application, CommandHandler

from telegram_bot.handlers.commands.pairing_commands import (
    pair_command,
    unpair_command,
    whoami_command,
)
from telegram_bot.handlers.commands.paper_commands import (
    briefing_command,
    inbox_command,
    next_command,
    papers_command,
    stats_command,
)
from telegram_bot.handlers.commands.project_commands import newproject_command, projects_command
from telegram_bot.handlers.commands.system_commands import (
    focus_command,
    help_command,
    pulse_now_command,
    start_command,
)
from telegram_bot.handlers.commands.task_commands import done_command, tasks_command


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
    app.add_handler(CommandHandler("inbox", inbox_command))
    app.add_handler(CommandHandler("pulse_now", pulse_now_command))
    # Sprint A: per-user Telegram pairing commands
    app.add_handler(CommandHandler("pair", pair_command))
    app.add_handler(CommandHandler("unpair", unpair_command))
    app.add_handler(CommandHandler("whoami", whoami_command))
