"""Guard: cross-service auth-DI boundary for learning_engine.

The Telegram bot calls a small, fixed set of LE endpoints *per user* with
``X-API-Key`` + ``X-Jarvis-Paired-User-Id`` (no session cookie). Those endpoints MUST
resolve identity via ``current_user_id_strict`` so the
header-authenticated owner is honored — a session-only resolver would 401 the
bot.

Conversely, the LE routers the Telegram bot never reaches per-user
(``cards``, ``analytics``, ``decks``, …) are intentionally left on session-only
``current_user_id_strict``. Converting them would add an unused header-spoofing
surface with no caller to justify it. The session-only choice is *by design*,
not a gap — this file pins that intent. The DELETE task/project handlers also
stay session-only on purpose (pinned below to guard against accidental
widening alongside their read/update siblings).

Telegram→LE call sites (grounded at HEAD 2026-06-02):
- ``GET  /api/review/next``           -> review.get_next_review
  (services/telegram_bot/.../handlers/review_handler.py)
- ``POST /api/review/{card_id:int}``  -> review.submit_review
  (services/telegram_bot/.../handlers/review_handler.py)
- ``GET  /api/stats``                 -> review.get_stats
  (services/telegram_bot/.../handlers/commands/paper_commands.py,
   orchestration/daily_briefing.py, orchestration/review_reminder.py)
- ``POST /api/executive/focus/log``   -> executive.log_focus_session
  (services/telegram_bot/.../handlers/commands/system_commands.py)
- ``GET  /api/projects``              -> projects.list_projects
- ``POST /api/projects``              -> projects.create_project
- ``GET  /api/projects/{id}``         -> projects.get_project
- ``GET  /api/projects/{id}/tasks``   -> tasks.list_tasks
- ``PUT  /api/tasks/{id}``            -> tasks.update_task
- ``GET  /api/tasks``                 -> tasks.list_all_tasks (cross-project)
- ``GET  /api/projects/{id}/milestones`` -> milestones.list_milestones
- ``GET  /api/milestones/upcoming``   -> milestones.list_upcoming_milestones
  (the project/task/milestone group is reached by the Telegram bot per-user
   with X-Jarvis-Paired-User-Id once the REST-decoupling lands.)

The handlers carry an ``@limiter.limit`` decorator, so the undecorated
function is reached via ``.__wrapped__`` (mirrors test_review_sync.py).
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import cast
from unittest.mock import AsyncMock

import asyncpg
import pytest
from fastapi import FastAPI, Request, Response
from fastapi.testclient import TestClient
from fastapi.routing import APIRoute
from jarvis_common.auth import (
    current_user_id_strict,
)
from jarvis_common.testing import make_pool_and_conn
from learning_engine.deps import get_db_pool
from learning_engine.routers import (
    cards,
    executive,
    internal_telegram,
    milestones,
    projects,
    review,
    tasks,
)


def _user_id_dep(func):
    """Return the ``user_id`` parameter's ``Depends(...)`` default.

    Unwraps the ``@limiter.limit`` decorator first so the real handler
    signature (with the FastAPI dependency default) is inspected.
    """
    target = getattr(func, "__wrapped__", func)
    return inspect.signature(target).parameters["user_id"].default


def _nudge_client(
    *,
    principal: str | None = "telegram",
    fetch_return: object | None = None,
    fetchval_return: object | None = None,
) -> tuple[TestClient, AsyncMock]:
    """Build the Learning nudge router with deterministic identity and storage."""
    pool, conn = make_pool_and_conn(
        fetch_return=[] if fetch_return is None else fetch_return,
        fetchval_return=fetchval_return,
        direct_methods=True,
    )
    app = FastAPI()
    app.include_router(internal_telegram.router)

    @app.middleware("http")
    async def attach_principal(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if principal is not None:
            request.state.identity_principal = principal
        return await call_next(request)

    def pool_override() -> asyncpg.Pool:
        return cast(asyncpg.Pool, pool)

    app.dependency_overrides[get_db_pool] = pool_override
    return TestClient(app), conn


def test_learning_lists_only_owner_local_enabled_nudges() -> None:
    client, conn = _nudge_client(
        fetch_return=[{"id": 4, "nudge_type": "review", "cron_expression": "0 9 * * *"}],
    )

    response = client.get("/internal/telegram/nudges")

    assert response.status_code == 200
    assert response.json() == [{"id": 4, "nudge_type": "review", "cron_expression": "0 9 * * *"}]
    conn.fetch.assert_awaited_once()


@pytest.mark.parametrize(("updated", "expected_status"), [(4, 204), (None, 404)])
def test_learning_nudge_acknowledgement_is_explicit(
    updated: int | None,
    expected_status: int,
) -> None:
    client, conn = _nudge_client(fetchval_return=updated)

    response = client.post("/internal/telegram/nudges/4/ack")

    assert response.status_code == expected_status
    conn.fetchval.assert_awaited_once()


def test_learning_nudges_reject_non_telegram_principal() -> None:
    client, conn = _nudge_client(principal="research")

    response = client.get("/internal/telegram/nudges")

    assert response.status_code == 403
    conn.fetch.assert_not_awaited()


# ---------------------------------------------------------------------------
# Telegram-reachable Learning endpoints require verified ASGI identity
# ---------------------------------------------------------------------------


def test_get_next_review_uses_strict_identity() -> None:
    """GET /api/review/next reads only identity verified for Learning."""
    assert _user_id_dep(review.get_next_review).dependency is current_user_id_strict


def test_submit_review_uses_strict_identity() -> None:
    """POST /api/review/{card_id:int} reads only verified identity."""
    assert _user_id_dep(review.submit_review).dependency is current_user_id_strict


def test_get_stats_uses_strict_identity() -> None:
    """GET /api/stats reads only identity verified for Learning."""
    assert _user_id_dep(review.get_stats).dependency is current_user_id_strict


def test_log_focus_session_uses_strict_identity() -> None:
    """POST /api/executive/focus/log reads only verified identity."""
    assert _user_id_dep(executive.log_focus_session).dependency is current_user_id_strict


def test_telegram_reachable_le_routes_registered() -> None:
    """Pin the exact route paths the Telegram bot depends on."""
    review_paths = {
        (r.path, tuple(sorted(r.methods)))
        for r in review.router.routes
        if isinstance(r, APIRoute) and r.methods is not None
    }
    exec_paths = {
        (r.path, tuple(sorted(r.methods)))
        for r in executive.router.routes
        if isinstance(r, APIRoute) and r.methods is not None
    }
    assert ("/api/review/next", ("GET",)) in review_paths
    assert ("/api/review/{card_id:int}", ("POST",)) in review_paths
    assert ("/api/stats", ("GET",)) in review_paths
    assert ("/api/executive/focus/log", ("POST",)) in exec_paths


# ---------------------------------------------------------------------------
# Intentionally session-only LE router: stays on current_user_id_strict
# ---------------------------------------------------------------------------


def test_cards_router_uses_strict_identity() -> None:
    """Card listing accepts only verified ASGI identity."""
    dep = _user_id_dep(cards.list_cards)
    assert dep.dependency is current_user_id_strict


def test_quick_add_task_uses_strict_identity() -> None:
    """POST /api/executive/tasks accepts only verified ASGI identity."""
    dep = _user_id_dep(executive.quick_add_task)
    assert dep.dependency is current_user_id_strict


# ---------------------------------------------------------------------------
# Executive My Day routes use strict verified identity
# ---------------------------------------------------------------------------


def test_get_my_day_uses_strict_identity() -> None:
    """GET /api/executive/my-day reads only verified identity."""
    assert _user_id_dep(executive.get_my_day).dependency is current_user_id_strict


def test_get_my_day_bundle_uses_strict_identity() -> None:
    """GET /api/executive/my-day/bundle reads only verified identity."""
    assert _user_id_dep(executive.get_my_day_bundle).dependency is current_user_id_strict


# ---------------------------------------------------------------------------
# Project and task endpoints use strict verified identity
# ---------------------------------------------------------------------------


def test_list_projects_uses_strict_identity() -> None:
    """GET /api/projects reads only verified identity."""
    assert _user_id_dep(projects.list_projects).dependency is current_user_id_strict


def test_create_project_uses_strict_identity() -> None:
    """POST /api/projects reads only verified identity."""
    assert _user_id_dep(projects.create_project).dependency is current_user_id_strict


def test_get_project_uses_strict_identity() -> None:
    """GET /api/projects/{id} reads only verified identity."""
    assert _user_id_dep(projects.get_project).dependency is current_user_id_strict


def test_list_tasks_uses_strict_identity() -> None:
    """GET /api/projects/{id}/tasks reads only verified identity."""
    assert _user_id_dep(tasks.list_tasks).dependency is current_user_id_strict


def test_update_task_uses_strict_identity() -> None:
    """PUT /api/tasks/{id} reads only verified identity."""
    assert _user_id_dep(tasks.update_task).dependency is current_user_id_strict


def test_list_all_tasks_uses_strict_identity() -> None:
    """GET /api/tasks reads only verified identity."""
    assert _user_id_dep(tasks.list_all_tasks).dependency is current_user_id_strict


def test_list_milestones_uses_strict_identity() -> None:
    """GET /api/projects/{id}/milestones reads only verified identity."""
    assert _user_id_dep(milestones.list_milestones).dependency is current_user_id_strict


def test_list_upcoming_milestones_uses_strict_identity() -> None:
    """GET /api/milestones/upcoming reads only verified identity."""
    assert _user_id_dep(milestones.list_upcoming_milestones).dependency is current_user_id_strict


# ---------------------------------------------------------------------------
# Destructive endpoints use the same strict verified identity seam
# ---------------------------------------------------------------------------


def test_delete_task_uses_strict_identity() -> None:
    """DELETE /api/tasks/{id} accepts only verified ASGI identity."""
    dep = _user_id_dep(tasks.delete_task)
    assert dep.dependency is current_user_id_strict


def test_delete_project_uses_strict_identity() -> None:
    """DELETE /api/projects/{id} accepts only verified ASGI identity."""
    dep = _user_id_dep(projects.delete_project)
    assert dep.dependency is current_user_id_strict
