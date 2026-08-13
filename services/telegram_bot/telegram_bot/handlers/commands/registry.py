"""Command handler registration for the JARVIS Telegram bot."""

from __future__ import annotations

from telegram.ext import Application, CommandHandler

from telegram_bot.command_catalog import standard_command_specs
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

STANDARD_COMMAND_HANDLERS = {
    "start": start_command,
    "help": help_command,
    "papers": papers_command,
    "stats": stats_command,
    "briefing": briefing_command,
    "projects": projects_command,
    "tasks": tasks_command,
    "done": done_command,
    "newproject": newproject_command,
    "focus": focus_command,
    "next": next_command,
    "inbox": inbox_command,
    "pulse_now": pulse_now_command,
    "pair": pair_command,
    "unpair": unpair_command,
    "whoami": whoami_command,
}

if set(STANDARD_COMMAND_HANDLERS) != {spec.name for spec in standard_command_specs()}:
    raise RuntimeError("Telegram command catalog and standard handlers must match")


def register_command_handlers(app: Application) -> None:
    """Register all command handlers on the given application.

    Parameters
    ----------
    app : Application
        The ``python-telegram-bot`` Application instance.
    """
    for spec in standard_command_specs():
        app.add_handler(CommandHandler(spec.name, STANDARD_COMMAND_HANDLERS[spec.name]))
