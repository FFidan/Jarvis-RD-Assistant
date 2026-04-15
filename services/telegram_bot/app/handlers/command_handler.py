# DEPRECATED — use handlers.commands.*
# This module is a thin re-export stub kept for backward compatibility.
# All command implementations live in app.handlers.commands.*

from app.handlers.commands import (
    briefing_command,
    done_command,
    focus_command,
    help_command,
    newproject_command,
    next_command,
    papers_command,
    projects_command,
    pulse_now_command,
    register_command_handlers,
    start_command,
    stats_command,
    tasks_command,
)
from app.handlers.commands.system_commands import _handle_pairing

__all__ = [
    "briefing_command",
    "done_command",
    "focus_command",
    "help_command",
    "newproject_command",
    "next_command",
    "papers_command",
    "projects_command",
    "pulse_now_command",
    "register_command_handlers",
    "start_command",
    "stats_command",
    "tasks_command",
    "_handle_pairing",
]
