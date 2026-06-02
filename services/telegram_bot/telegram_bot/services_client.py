"""Thin typed REST client for all product-data calls the bot makes to backend services.

Each function is a pure transport + parse layer: it builds the canonical
``_owner_headers``, issues exactly one HTTP call, calls
``resp.raise_for_status()`` (propagating ``httpx.HTTPStatusError`` to callers),
and returns parsed JSON.  **No business logic** lives here.

Callers are responsible for:
- Resolving ``user_id`` to a concrete ``int`` before calling (no ``None`` accepted).
- Catching ``httpx.HTTPStatusError`` / ``httpx.HTTPError`` for user-facing error
  messages (handlers) or silent-skip logic (orchestration).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from telegram_bot.config import BotConfig
from telegram_bot.handlers.helpers import _owner_headers

__all__ = [
    "fetch_projects",
    "fetch_project",
    "fetch_project_tasks",
    "fetch_project_milestones",
    "fetch_tasks",
    "create_project",
    "complete_task",
    "fetch_upcoming_milestones",
    "fetch_due_card_count",
    "fetch_new_paper_count",
    "check_authors",
]


# ---------------------------------------------------------------------------
# Learning Engine — project / task / milestone / stats
# ---------------------------------------------------------------------------


async def fetch_projects(
    http: httpx.AsyncClient,
    config: BotConfig,
    user_id: int,
    *,
    status: str | None = None,
) -> list[dict[str, Any]]:
    """GET {learning_engine}/api/projects[?status=].

    Parameters
    ----------
    status:
        Optional status filter (e.g. ``"active"``).  Omitted when ``None``.
    """
    params: dict[str, str] = {}
    if status is not None:
        params["status"] = status
    resp = await http.get(
        f"{config.learning_engine_url}/api/projects",
        params=params or None,
        headers=_owner_headers(config, user_id),
    )
    resp.raise_for_status()
    result: list[dict[str, Any]] = resp.json()
    return result


async def fetch_project(
    http: httpx.AsyncClient,
    config: BotConfig,
    user_id: int,
    project_id: int,
) -> dict[str, Any] | None:
    """GET {learning_engine}/api/projects/{project_id}.

    Returns ``None`` on 404; re-raises any other ``httpx.HTTPStatusError``.
    """
    resp = await http.get(
        f"{config.learning_engine_url}/api/projects/{project_id}",
        headers=_owner_headers(config, user_id),
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    result: dict[str, Any] = resp.json()
    return result


async def fetch_project_tasks(
    http: httpx.AsyncClient,
    config: BotConfig,
    user_id: int,
    project_id: int,
) -> list[dict[str, Any]]:
    """GET {learning_engine}/api/projects/{project_id}/tasks."""
    resp = await http.get(
        f"{config.learning_engine_url}/api/projects/{project_id}/tasks",
        headers=_owner_headers(config, user_id),
    )
    resp.raise_for_status()
    result: list[dict[str, Any]] = resp.json()
    return result


async def fetch_project_milestones(
    http: httpx.AsyncClient,
    config: BotConfig,
    user_id: int,
    project_id: int,
) -> list[dict[str, Any]]:
    """GET {learning_engine}/api/projects/{project_id}/milestones."""
    resp = await http.get(
        f"{config.learning_engine_url}/api/projects/{project_id}/milestones",
        headers=_owner_headers(config, user_id),
    )
    resp.raise_for_status()
    result: list[dict[str, Any]] = resp.json()
    return result


async def fetch_tasks(
    http: httpx.AsyncClient,
    config: BotConfig,
    user_id: int,
    *,
    status: str | None = None,
    project_id: int | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """GET {learning_engine}/api/tasks[?status=&project_id=&limit=].

    Parameters
    ----------
    status:
        Optional status filter (e.g. ``"in_progress"``).
    project_id:
        Optional project scope.
    limit:
        Maximum number of tasks to return (default 50).
    """
    params: dict[str, str | int] = {"limit": limit}
    if status is not None:
        params["status"] = status
    if project_id is not None:
        params["project_id"] = project_id
    resp = await http.get(
        f"{config.learning_engine_url}/api/tasks",
        params=params,
        headers=_owner_headers(config, user_id),
    )
    resp.raise_for_status()
    result: list[dict[str, Any]] = resp.json()
    return result


async def create_project(
    http: httpx.AsyncClient,
    config: BotConfig,
    user_id: int,
    *,
    name: str,
    description: str | None = None,
    deadline: str | None = None,
) -> dict[str, Any]:
    """POST {learning_engine}/api/projects.

    Parameters
    ----------
    name:
        Project name (required).
    description:
        Optional project description.
    deadline:
        Optional ISO date/datetime string for the project deadline.
    """
    body: dict[str, Any] = {"name": name}
    if description is not None:
        body["description"] = description
    if deadline is not None:
        body["deadline"] = deadline
    resp = await http.post(
        f"{config.learning_engine_url}/api/projects",
        json=body,
        headers=_owner_headers(config, user_id),
    )
    resp.raise_for_status()
    result: dict[str, Any] = resp.json()
    return result


async def complete_task(
    http: httpx.AsyncClient,
    config: BotConfig,
    user_id: int,
    task_id: int,
) -> dict[str, Any] | None:
    """PUT {learning_engine}/api/tasks/{task_id} body {"status": "done"}.

    Returns ``None`` on 404; re-raises any other ``httpx.HTTPStatusError``.
    """
    resp = await http.put(
        f"{config.learning_engine_url}/api/tasks/{task_id}",
        json={"status": "done"},
        headers=_owner_headers(config, user_id),
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    result: dict[str, Any] = resp.json()
    return result


async def fetch_upcoming_milestones(
    http: httpx.AsyncClient,
    config: BotConfig,
    user_id: int,
    *,
    within_days: int,
) -> list[dict[str, Any]]:
    """GET {learning_engine}/api/milestones/upcoming?within_days=.

    **R5 — deadline parsing:** each item's ``deadline`` string is parsed back
    to a ``datetime`` via ``datetime.fromisoformat`` before returning, because
    the bot's formatters do ``isinstance(deadline, datetime)`` date-math and
    would mis-render a raw ISO string.
    """
    resp = await http.get(
        f"{config.learning_engine_url}/api/milestones/upcoming",
        params={"within_days": within_days},
        headers=_owner_headers(config, user_id),
    )
    resp.raise_for_status()
    items: list[dict[str, Any]] = resp.json()
    for item in items:
        raw = item.get("deadline")
        if isinstance(raw, str):
            item["deadline"] = datetime.fromisoformat(raw)
    return items


async def fetch_due_card_count(
    http: httpx.AsyncClient,
    config: BotConfig,
    user_id: int,
) -> int:
    """GET {learning_engine}/api/stats → resp["due_now"].

    Returns the integer count of due flashcards.
    """
    resp = await http.get(
        f"{config.learning_engine_url}/api/stats",
        headers=_owner_headers(config, user_id),
    )
    resp.raise_for_status()
    data: dict[str, Any] = resp.json()
    return int(data["due_now"])


# ---------------------------------------------------------------------------
# Paper Ingestion — feed / author checks
# ---------------------------------------------------------------------------


async def fetch_new_paper_count(
    http: httpx.AsyncClient,
    config: BotConfig,
    user_id: int,
    *,
    hours: int = 24,
) -> int:
    """GET {paper_ingestion}/api/papers/feed?date_from=<ISO now-hours>&limit=1 → resp["total"].

    **R6 — day-granularity note:** ``date_from`` is sent as an ISO datetime
    (UTC now − *hours*), but the feed endpoint treats it as a date, so the
    effective window is day-granular.  This is acceptable for a briefing stat.

    Returns
    -------
    int
        Total count of new papers found since ``now - hours``.
    """
    since = datetime.now(UTC) - timedelta(hours=hours)
    resp = await http.get(
        f"{config.paper_ingestion_url}/api/papers/feed",
        params={"date_from": since.isoformat(), "limit": 1},
        headers=_owner_headers(config, user_id),
    )
    resp.raise_for_status()
    data: dict[str, Any] = resp.json()
    return int(data["total"])


async def check_authors(
    http: httpx.AsyncClient,
    config: BotConfig,
    user_id: int,
) -> dict[str, Any]:
    """POST {paper_ingestion}/api/authors/check.

    Returns
    -------
    dict
        Expected keys: ``matches``, ``new_papers``, ``authors_checked``.
    """
    resp = await http.post(
        f"{config.paper_ingestion_url}/api/authors/check",
        headers=_owner_headers(config, user_id),
    )
    resp.raise_for_status()
    result: dict[str, Any] = resp.json()
    return result
