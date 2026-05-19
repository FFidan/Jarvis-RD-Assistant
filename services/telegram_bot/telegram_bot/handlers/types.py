"""Shared TypedDict definitions for Telegram handler payloads.

These mirror the DB row shapes used by project_commands, task_commands, and
callback_handler.  Using TypedDict (not dataclass) keeps the values
dict-compatible — existing ``dict[row]`` casts continue to work, and pyright
catches key-name typos without any runtime change.
"""

from __future__ import annotations

from datetime import datetime
from typing import NotRequired, TypedDict


class ProjectRow(TypedDict):
    """Row shape returned by ``SELECT … FROM projects``."""

    id: int
    name: str
    status: str  # 'active' | 'paused' | 'completed' | 'archived'
    description: NotRequired[str | None]
    deadline: NotRequired[datetime | None]
    color: NotRequired[str | None]
    created_at: NotRequired[datetime]
    updated_at: NotRequired[datetime]


class TaskRow(TypedDict):
    """Row shape returned by ``SELECT … FROM tasks`` (may include project_name join)."""

    id: int
    title: str
    status: str  # 'todo' | 'in_progress' | 'blocked' | 'done'
    project_id: NotRequired[int | None]
    parent_task_id: NotRequired[int | None]
    description: NotRequired[str | None]
    priority: NotRequired[int]
    deadline: NotRequired[datetime | None]
    estimated_hours: NotRequired[float | None]
    actual_hours: NotRequired[float | None]
    sort_order: NotRequired[int]
    completed_at: NotRequired[datetime | None]
    created_at: NotRequired[datetime]
    updated_at: NotRequired[datetime]
    # JOIN column: present when queried with LEFT JOIN projects
    project_name: NotRequired[str | None]
