"""Shared display vocabulary for the bot's user-facing surfaces.

One definition per concept, so a project status or a task-completion rule reads
the same in every command that prints it.

``PROJECT_STATUS_LABELS`` mirrors the web app's
``frontend/src/lib/labels/projectStatus.ts``. The vocabulary has to cross a
language boundary, so the duplication is unavoidable; a contract test pins the
two maps equal instead.
"""

from __future__ import annotations

from typing import Final

#: Stored project status -> the label the user reads. Mirrors the web app.
PROJECT_STATUS_LABELS: Final[dict[str, str]] = {
    "active": "In progress",
    "paused": "Draft",
    "completed": "Completed",
    "archived": "Archived",
}

#: Stored project status -> its badge glyph, matching the web app's status dots.
PROJECT_STATUS_EMOJI: Final[dict[str, str]] = {
    "active": "🟢",
    "paused": "⏸️",
    "completed": "✅",
    "archived": "📦",
}

#: Statuses hidden from the project list: archived projects are put away
#: deliberately, every other status is still live work.
ARCHIVED_PROJECT_STATUS: Final[str] = "archived"

#: Task statuses that count as "not done".
#:
#: The executive My Day view selects tasks whose status is ``todo``,
#: ``in_progress`` or ``blocked`` (``done`` tasks appear there only as the
#: day's completions), so every bot surface that counts outstanding work uses
#: this same set rather than a narrower one.
NOT_DONE_TASK_STATUSES: Final[frozenset[str]] = frozenset({"todo", "in_progress", "blocked"})


def project_status_label(status: str | None) -> str:
    """Translate a stored project status into the label the user reads.

    Parameters
    ----------
    status : str | None
        Stored status value, e.g. ``"paused"``.

    Returns
    -------
    str
        The shared display label, or the raw value when it is not a known
        status (callers are responsible for escaping it).
    """
    if not status:
        return ""
    return PROJECT_STATUS_LABELS.get(status, status)


def project_status_emoji(status: str | None) -> str:
    """Return the badge glyph for a stored project status, or ``""`` if unknown.

    Parameters
    ----------
    status : str | None
        Stored status value, e.g. ``"paused"``.

    Returns
    -------
    str
        The badge glyph, or an empty string for an unrecognized status.
    """
    if not status:
        return ""
    return PROJECT_STATUS_EMOJI.get(status, "")


def is_not_done(task: dict) -> bool:
    """Report whether a task row counts as outstanding under the My Day rule.

    Parameters
    ----------
    task : dict
        Task row from the Learning Engine ``/api/tasks`` response.

    Returns
    -------
    bool
        True when the task's status is one of :data:`NOT_DONE_TASK_STATUSES`.
    """
    return task.get("status") in NOT_DONE_TASK_STATUSES
