"""Single user-facing contract for Telegram commands."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

CommandOwner = Literal["standard", "review"]
HelpGroup = Literal["Papers", "Learning", "Projects and tasks", "Account", "General"]


@dataclass(frozen=True, slots=True)
class CommandSpec:
    """One command's public name, usage, help placement, and handler owner."""

    name: str
    usage: str
    description: str
    group: HelpGroup
    owner: CommandOwner
    include_in_menu: bool = True


COMMAND_CATALOG: tuple[CommandSpec, ...] = (
    CommandSpec("papers", "papers [query]", "List recent papers or search", "Papers", "standard"),
    CommandSpec("briefing", "briefing", "Show the current briefing", "Papers", "standard"),
    CommandSpec("next", "next", "Show the next Pulse recommendation", "Papers", "standard"),
    CommandSpec("inbox", "inbox", "Show unread saved papers", "Papers", "standard"),
    CommandSpec("pulse_now", "pulse_now", "Run Pulse discovery now", "Papers", "standard"),
    CommandSpec("review", "review", "Start flashcard review", "Learning", "review"),
    CommandSpec("stats", "stats", "Show learning statistics", "Learning", "standard"),
    CommandSpec(
        "cancel",
        "cancel",
        "Cancel the current flashcard review",
        "Learning",
        "review",
        include_in_menu=False,
    ),
    CommandSpec("projects", "projects", "List active projects", "Projects and tasks", "standard"),
    CommandSpec(
        "newproject",
        "newproject <name>",
        "Create a project",
        "Projects and tasks",
        "standard",
    ),
    CommandSpec("tasks", "tasks", "List in-progress tasks", "Projects and tasks", "standard"),
    CommandSpec("done", "done <id>", "Mark a task complete", "Projects and tasks", "standard"),
    CommandSpec(
        "focus",
        "focus [minutes]",
        "Start a focus session",
        "Projects and tasks",
        "standard",
    ),
    CommandSpec("pair", "pair <code>", "Pair this chat to your account", "Account", "standard"),
    CommandSpec("unpair", "unpair", "Unlink this chat from your account", "Account", "standard"),
    CommandSpec("whoami", "whoami", "Show the paired account", "Account", "standard"),
    CommandSpec("help", "help", "Show command help", "General", "standard"),
    CommandSpec("start", "start", "Show the welcome message", "General", "standard"),
)

_BY_NAME = {spec.name: spec for spec in COMMAND_CATALOG}
if len(_BY_NAME) != len(COMMAND_CATALOG):
    raise RuntimeError("Telegram command names must be unique")


def command_spec(name: str) -> CommandSpec:
    """Return one catalog entry by its exact command name."""
    return _BY_NAME[name]


def standard_command_specs() -> tuple[CommandSpec, ...]:
    """Return commands owned by the ordinary command registry."""
    return tuple(spec for spec in COMMAND_CATALOG if spec.owner == "standard")


def menu_command_specs() -> tuple[CommandSpec, ...]:
    """Return commands eligible for Telegram's autocomplete menu."""
    return tuple(spec for spec in COMMAND_CATALOG if spec.include_in_menu)
