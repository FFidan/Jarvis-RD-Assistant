"""Command handlers package for the JARVIS Telegram bot.

Re-exports all command handler functions and the registration entry-point so
that existing import paths keep working:

    from app.handlers.commands import papers_command, register_command_handlers
"""

from app.handlers.commands.paper_commands import (
    briefing_command,
    next_command,
    papers_command,
    stats_command,
)
from app.handlers.commands.project_commands import (
    newproject_command,
    projects_command,
)
from app.handlers.commands.registry import register_command_handlers
from app.handlers.commands.system_commands import (
    focus_command,
    help_command,
    pulse_now_command,
    start_command,
)
from app.handlers.commands.task_commands import (
    done_command,
    tasks_command,
)

__all__ = [
    # paper domain
    "papers_command",
    "stats_command",
    "briefing_command",
    "next_command",
    # project domain
    "projects_command",
    "newproject_command",
    # task domain
    "tasks_command",
    "done_command",
    # system domain
    "start_command",
    "help_command",
    "focus_command",
    "pulse_now_command",
    # registration
    "register_command_handlers",
]
