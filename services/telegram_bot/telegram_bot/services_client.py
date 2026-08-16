"""Thin typed REST client for most product-data calls the bot makes to backend services.

Each function is a pure transport + parse layer: it builds the canonical
``_owner_headers``, issues exactly one HTTP call, calls
``resp.raise_for_status()`` (propagating ``httpx.HTTPStatusError`` to callers),
and returns parsed JSON.  **No business logic** lives here.

Callers are responsible for:
- Resolving ``user_id`` to a concrete ``int`` before calling.
- Catching ``httpx.HTTPStatusError`` / ``httpx.HTTPError`` for user-facing error
  messages (handlers) or silent-skip logic (orchestration).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ValidationError

from telegram_bot.config import BotConfig, _owner_headers
from telegram_bot.focus_contract import FocusSession, FocusTransition
from telegram_bot.pulse_contract import PulseDeck, PulseGenerateJob

__all__ = [
    "PulsePayloadError",
    "fetch_projects",
    "fetch_project",
    "fetch_project_tasks",
    "fetch_project_milestones",
    "fetch_tasks",
    "create_project",
    "complete_task",
    "fetch_upcoming_milestones",
    "fetch_due_card_count",
    "fetch_stats",
    "fetch_next_review_card",
    "submit_review_rating",
    "fetch_active_focus_session",
    "fetch_pending_telegram_focus_completion",
    "start_focus_session",
    "pause_focus_session",
    "resume_focus_session",
    "complete_focus_session",
    "acknowledge_telegram_focus_completion",
    "log_focus_session",
    "fetch_new_paper_count",
    "check_authors",
    "search_papers",
    "fetch_papers_feed",
    "search_papers_feed",
    "get_paper",
    "update_paper_action",
    "record_paper_feedback",
    "fetch_pulse_today",
    "trigger_pulse_generation",
    "fetch_weekly_digest",
    "ScheduledNudgePayload",
    "acknowledge_scheduled_nudge",
    "fetch_scheduled_nudges",
]


class PulsePayloadError(ValueError):
    """Sanitized boundary error for a malformed Pulse response."""


class ScheduledNudgePayload(BaseModel):
    """Learning-owned enabled nudge schedule returned to Telegram."""

    id: int
    nudge_type: str
    cron_expression: str


async def fetch_scheduled_nudges(
    http: httpx.AsyncClient,
    config: BotConfig,
    user_id: int,
) -> list[ScheduledNudgePayload]:
    """Return enabled nudge schedules from Learning.

    Parameters
    ----------
    http : httpx.AsyncClient
        Scoped backend client.
    config : BotConfig
        Runtime service origins.
    user_id : int
        Platform-verified paired owner used for the service assertion.

    Returns
    -------
    list[ScheduledNudgePayload]
        Validated enabled schedules.
    """
    response = await http.get(
        f"{config.learning_engine_url}/internal/telegram/nudges",
        headers=_owner_headers(config, user_id),
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise RuntimeError("Learning returned an invalid nudge list")
    return [ScheduledNudgePayload.model_validate(item) for item in payload]


async def acknowledge_scheduled_nudge(
    http: httpx.AsyncClient,
    config: BotConfig,
    user_id: int,
    nudge_id: int,
) -> None:
    """Record one successful nudge execution in Learning.

    Parameters
    ----------
    http : httpx.AsyncClient
        Scoped backend client.
    config : BotConfig
        Runtime service origins.
    user_id : int
        Platform-verified paired owner used for the service assertion.
    nudge_id : int
        Learning-owned schedule identifier.
    """
    response = await http.post(
        f"{config.learning_engine_url}/internal/telegram/nudges/{nudge_id}/ack",
        headers=_owner_headers(config, user_id),
    )
    response.raise_for_status()


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


async def fetch_stats(
    http: httpx.AsyncClient,
    config: BotConfig,
    user_id: int,
    *,
    timeout: float | None = None,
) -> dict[str, Any]:
    """GET {learning_engine}/api/stats.

    Returns the raw stats payload (``total_cards``, ``due_now``,
    ``reviewed_today``, ``average_retention``, ``streak_days``).

    Parameters
    ----------
    timeout:
        Optional per-call override.  Omitted (``None``) uses the shared
        client's own default timeout.
    """
    kwargs: dict[str, Any] = {"headers": _owner_headers(config, user_id)}
    if timeout is not None:
        kwargs["timeout"] = timeout
    resp = await http.get(f"{config.learning_engine_url}/api/stats", **kwargs)
    resp.raise_for_status()
    result: dict[str, Any] = resp.json()
    return result


async def fetch_next_review_card(
    http: httpx.AsyncClient,
    config: BotConfig,
    user_id: int,
) -> list[dict[str, Any]]:
    """GET {learning_engine}/api/review/next?limit=1.

    Returns the raw list payload (empty when no cards are due).
    """
    resp = await http.get(
        f"{config.learning_engine_url}/api/review/next",
        params={"limit": 1},
        headers=_owner_headers(config, user_id),
        timeout=15.0,
    )
    resp.raise_for_status()
    result: list[dict[str, Any]] = resp.json()
    return result


async def submit_review_rating(
    http: httpx.AsyncClient,
    config: BotConfig,
    user_id: int,
    card_id: int,
    rating: int,
) -> dict[str, Any]:
    """POST {learning_engine}/api/review/{card_id} body {"rating": rating}.

    Returns the parsed review result (``next_due_at``, ...).
    """
    resp = await http.post(
        f"{config.learning_engine_url}/api/review/{card_id}",
        json={"rating": rating},
        headers=_owner_headers(config, user_id),
        timeout=15.0,
    )
    resp.raise_for_status()
    result: dict[str, Any] = resp.json()
    return result


async def log_focus_session(
    http: httpx.AsyncClient,
    config: BotConfig,
    user_id: int | None,
    duration_hours: float,
) -> None:
    """POST {learning_engine}/api/executive/focus/log body {"duration_hours": ...}.

    Fire-and-forget (best-effort scheduled-job callback); the caller only
    needs success/failure, never the response body.  Unlike other functions
    here, *user_id* accepts ``None`` — the scheduled job's stored data may not
    carry an owner id.
    """
    resp = await http.post(
        f"{config.learning_engine_url}/api/executive/focus/log",
        json={"duration_hours": duration_hours},
        headers=_owner_headers(config, user_id),
        timeout=10.0,
    )
    resp.raise_for_status()


def _parse_focus_session(payload: object) -> FocusSession:
    """Validate one focus payload without exposing malformed values."""
    try:
        return FocusSession.model_validate(payload)
    except ValidationError as exc:
        raise ValueError("Learning Engine returned an invalid focus session") from exc


def _parse_focus_transition(payload: object) -> FocusTransition:
    """Validate one focus transition without exposing malformed values."""
    try:
        return FocusTransition.model_validate(payload)
    except ValidationError as exc:
        raise ValueError("Learning Engine returned an invalid focus transition") from exc


async def fetch_active_focus_session(
    http: httpx.AsyncClient,
    config: BotConfig,
    user_id: int,
) -> FocusSession | None:
    """Return the user's open focus interval, or its just-completed transition."""
    resp = await http.get(
        f"{config.learning_engine_url}/api/executive/focus/active",
        headers=_owner_headers(config, user_id),
        timeout=10.0,
    )
    resp.raise_for_status()
    payload = resp.json()
    return None if payload is None else _parse_focus_session(payload)


async def fetch_pending_telegram_focus_completion(
    http: httpx.AsyncClient,
    config: BotConfig,
    user_id: int,
) -> FocusSession | None:
    """Return one durable, not-yet-acknowledged Telegram completion."""
    resp = await http.get(
        f"{config.learning_engine_url}/api/executive/focus/telegram/pending",
        headers=_owner_headers(config, user_id),
        timeout=10.0,
    )
    resp.raise_for_status()
    payload = resp.json()
    return None if payload is None else _parse_focus_session(payload)


async def start_focus_session(
    http: httpx.AsyncClient,
    config: BotConfig,
    user_id: int,
    duration_seconds: int,
) -> FocusSession:
    """Start one server-owned Telegram focus interval."""
    resp = await http.post(
        f"{config.learning_engine_url}/api/executive/focus/start",
        json={"duration_seconds": duration_seconds, "source": "telegram"},
        headers=_owner_headers(config, user_id),
        timeout=10.0,
    )
    resp.raise_for_status()
    return _parse_focus_session(resp.json())


async def pause_focus_session(
    http: httpx.AsyncClient,
    config: BotConfig,
    user_id: int,
    session_id: int,
) -> FocusTransition:
    """Pause a focus interval idempotently."""
    resp = await http.post(
        f"{config.learning_engine_url}/api/executive/focus/{session_id}/pause",
        headers=_owner_headers(config, user_id),
        timeout=10.0,
    )
    resp.raise_for_status()
    return _parse_focus_transition(resp.json())


async def resume_focus_session(
    http: httpx.AsyncClient,
    config: BotConfig,
    user_id: int,
    session_id: int,
) -> FocusTransition:
    """Resume a focus interval idempotently."""
    resp = await http.post(
        f"{config.learning_engine_url}/api/executive/focus/{session_id}/resume",
        headers=_owner_headers(config, user_id),
        timeout=10.0,
    )
    resp.raise_for_status()
    return _parse_focus_transition(resp.json())


async def complete_focus_session(
    http: httpx.AsyncClient,
    config: BotConfig,
    user_id: int,
    session_id: int,
    mode: Literal["elapsed", "stop"],
) -> FocusTransition:
    """Complete a focus interval idempotently."""
    resp = await http.post(
        f"{config.learning_engine_url}/api/executive/focus/{session_id}/complete",
        json={"mode": mode},
        headers=_owner_headers(config, user_id),
        timeout=10.0,
    )
    resp.raise_for_status()
    return _parse_focus_transition(resp.json())


async def acknowledge_telegram_focus_completion(
    http: httpx.AsyncClient,
    config: BotConfig,
    user_id: int,
    session_id: int,
) -> FocusTransition:
    """Acknowledge a completion only after Telegram accepted the message."""
    resp = await http.post(
        f"{config.learning_engine_url}/api/executive/focus/{session_id}/telegram-notified",
        headers=_owner_headers(config, user_id),
        timeout=10.0,
    )
    resp.raise_for_status()
    return _parse_focus_transition(resp.json())


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
    """GET {paper_ingestion}/api/papers/feed?date_from=<ISO date>&limit=1 → resp["total"].

    **R6 — day-granularity note:** the cutoff is computed as a datetime
    (UTC now − *hours*) but sent as an ISO **date** (the endpoint's
    ``date_from`` is a DATE param), so the effective window is day-granular.
    This is acceptable for a briefing stat.

    Returns
    -------
    int
        Total count of new papers found since ``now - hours``.
    """
    since = datetime.now(UTC) - timedelta(hours=hours)
    resp = await http.get(
        f"{config.paper_ingestion_url}/api/papers/feed",
        # /api/papers/feed's date_from is a DATE param — send a date string
        # (day granularity); a full datetime ISO string is rejected with 422.
        params={"date_from": since.date().isoformat(), "limit": 1},
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


#: Sources an external discovery search fans out to.  The endpoint's request
#: model defaults to arXiv alone, so omitting the field silently searches one
#: source; sources that are disabled or failing are the server's concern and
#: come back in ``degraded_sources``.
DISCOVERY_SOURCE_TYPES = ("arxiv", "semantic_scholar", "openalex", "pubmed")

#: External discovery fans out to four upstream APIs and persists what it
#: finds; 70.5 s has been observed end to end, so the client waits longer.
DISCOVERY_TIMEOUT_SECONDS = 90.0


async def search_papers(
    http: httpx.AsyncClient,
    config: BotConfig,
    user_id: int,
    query: str,
) -> dict[str, Any]:
    """POST {paper_ingestion}/api/search — external discovery across all sources.

    The endpoint also writes what it finds into the caller's library, so
    callers must tell the user about that side effect.

    Returns
    -------
    dict
        A ``MultiSourceSearchResponse``: ``results`` (everything the sources
        returned), ``total``, ``per_source_counts``, ``degraded_sources``,
        ``saved`` (rows persisted into the caller's library) and ``failed``.
    """
    resp = await http.post(
        f"{config.paper_ingestion_url}/api/search",
        json={"query": query, "source_types": list(DISCOVERY_SOURCE_TYPES)},
        headers=_owner_headers(config, user_id),
        timeout=DISCOVERY_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    result: dict[str, Any] = resp.json()
    return result


async def _get_papers_feed(
    http: httpx.AsyncClient,
    config: BotConfig,
    user_id: int,
    params: dict[str, Any],
) -> Any:
    """GET {paper_ingestion}/api/papers/feed with pre-built query params.

    *params* is the whole feed query (``view``, ``limit`` and optionally ``q``)
    so the public wrappers below stay small and share one transport.
    """
    resp = await http.get(
        f"{config.paper_ingestion_url}/api/papers/feed",
        params=params,
        headers=_owner_headers(config, user_id),
        timeout=30.0,
    )
    resp.raise_for_status()
    result: Any = resp.json()
    return result


async def fetch_papers_feed(
    http: httpx.AsyncClient,
    config: BotConfig,
    user_id: int,
    *,
    view: str,
    limit: int,
) -> Any:
    """GET {paper_ingestion}/api/papers/feed?view=&limit=.

    Returns the raw parsed JSON envelope (``{papers, total, search_mode}``);
    callers narrow the shape themselves.
    """
    return await _get_papers_feed(http, config, user_id, {"view": view, "limit": limit})


async def search_papers_feed(
    http: httpx.AsyncClient,
    config: BotConfig,
    user_id: int,
    query: str,
    *,
    limit: int = 10,
) -> Any:
    """Full-text search the caller's own library via the feed endpoint.

    Unlike :func:`search_papers` this reads existing papers only; it never
    reaches an external source and never writes to the library.
    """
    return await _get_papers_feed(
        http, config, user_id, {"view": "library", "limit": limit, "q": query}
    )


async def get_paper(
    http: httpx.AsyncClient,
    config: BotConfig,
    user_id: int,
    paper_id: int,
) -> dict[str, Any]:
    """GET {paper_ingestion}/api/papers/{paper_id}.

    Returns the raw detail payload (``paper``, ``summary``, ...).
    """
    resp = await http.get(
        f"{config.paper_ingestion_url}/api/papers/{paper_id}",
        headers=_owner_headers(config, user_id),
        timeout=15.0,
    )
    resp.raise_for_status()
    result: dict[str, Any] = resp.json()
    return result


async def update_paper_action(
    http: httpx.AsyncClient,
    config: BotConfig,
    user_id: int,
    paper_id: int,
    action: tuple[str, str],
) -> None:
    """{method} {paper_ingestion}/api/papers/{paper_id}/{suffix}.

    *action* is ``(method, suffix)`` — e.g. ``("PUT", "trash")``.
    Fire-and-forget lifecycle/curation action (save/skip/trash/star/...);
    the caller only needs success/failure, never the response body.
    """
    method, suffix = action
    resp = await http.request(
        method,
        f"{config.paper_ingestion_url}/api/papers/{paper_id}/{suffix}",
        headers=_owner_headers(config, user_id),
        timeout=15.0,
    )
    resp.raise_for_status()


async def record_paper_feedback(
    http: httpx.AsyncClient,
    config: BotConfig,
    user_id: int,
    paper_id: int,
    body: dict[str, str],
) -> None:
    """POST {paper_ingestion}/api/papers/{paper_id}/feedback body {"signal": ..., "source": ...}.

    *body* is ``{"signal": ..., "source": ...}``.  Fire-and-forget; the
    caller only needs success/failure.
    """
    resp = await http.post(
        f"{config.paper_ingestion_url}/api/papers/{paper_id}/feedback",
        json=body,
        headers=_owner_headers(config, user_id),
        timeout=15.0,
    )
    resp.raise_for_status()


async def fetch_pulse_today(
    http: httpx.AsyncClient,
    config: BotConfig,
    user_id: int,
    *,
    limit: int | None = None,
) -> PulseDeck | None:
    """GET {paper_ingestion}/api/pulse/today[?limit=].

    Returns the raw deck payload, or ``None`` when no deck exists for today
    (the endpoint responds 200 with a JSON ``null`` body in that case).
    """
    params: dict[str, int] = {}
    if limit is not None:
        params["limit"] = limit
    resp = await http.get(
        f"{config.paper_ingestion_url}/api/pulse/today",
        params=params or None,
        headers=_owner_headers(config, user_id),
        timeout=30.0,
    )
    resp.raise_for_status()
    payload: Any = resp.json()
    if payload is None:
        return None
    try:
        return PulseDeck.model_validate(payload)
    except ValidationError:
        raise PulsePayloadError("Pulse response did not match the expected contract") from None


async def trigger_pulse_generation(
    http: httpx.AsyncClient,
    config: BotConfig,
    user_id: int,
) -> PulseGenerateJob:
    """POST {paper_ingestion}/api/pulse/generate.

    Return the validated job identity so the enqueue cannot silently become an
    untrackable or malformed operation.
    """
    resp = await http.post(
        f"{config.paper_ingestion_url}/api/pulse/generate",
        headers=_owner_headers(config, user_id),
        timeout=15.0,
    )
    resp.raise_for_status()
    try:
        return PulseGenerateJob.model_validate(resp.json())
    except ValidationError:
        raise PulsePayloadError("Pulse job response did not match the expected contract") from None


async def fetch_weekly_digest(
    http: httpx.AsyncClient,
    config: BotConfig,
    user_id: int,
) -> dict[str, Any]:
    """GET {paper_ingestion}/api/digest/weekly?days=7.

    Returns the parsed digest payload (``topics``, ``total_papers``, ...).
    """
    resp = await http.get(
        f"{config.paper_ingestion_url}/api/digest/weekly",
        params={"days": 7},
        headers=_owner_headers(config, user_id),
        timeout=90.0,
    )
    resp.raise_for_status()
    result: dict[str, Any] = resp.json()
    return result
