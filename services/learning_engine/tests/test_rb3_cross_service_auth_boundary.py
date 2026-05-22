"""RB-3 guard: cross-service auth-DI boundary for learning_engine.

The Telegram bot calls a small, fixed set of LE endpoints *per user* with
``X-API-Key`` + ``X-Owner-User-Id`` (no session cookie). Those endpoints MUST
resolve identity via ``current_user_id_strict_with_owner_override`` so the
header-authenticated owner is honored — a session-only resolver would 401 the
bot.

Conversely, the LE routers the Telegram bot never reaches per-user
(``cards``, ``tasks``, ``analytics``, ``decks``, …) are intentionally left on
session-only ``current_user_id_strict``. Converting them would add an unused
header-spoofing surface with no caller to justify it. The session-only choice
is *by design*, not a gap — this file pins that intent.

Telegram→LE call sites (grounded at HEAD 2026-05-17):
- ``GET  /api/review/next``         -> review.get_next_review
  (services/telegram_bot/.../handlers/review_handler.py)
- ``POST /api/review/{card_id:int}`` -> review.submit_review
  (services/telegram_bot/.../handlers/review_handler.py)
- ``GET  /api/stats``               -> review.get_stats
  (services/telegram_bot/.../handlers/commands/paper_commands.py,
   orchestration/daily_briefing.py, orchestration/review_reminder.py)
- ``POST /api/executive/focus/log`` -> executive.log_focus_session
  (services/telegram_bot/.../handlers/commands/system_commands.py)

The handlers carry an ``@limiter.limit`` decorator, so the undecorated
function is reached via ``.__wrapped__`` (mirrors test_review_sync.py).
"""

from __future__ import annotations

import inspect

from fastapi.routing import APIRoute
from jarvis_common.auth import (
    current_user_id_strict,
    current_user_id_strict_with_owner_override,
)
from learning_engine.routers import cards, executive, review


def _user_id_dep(func):
    """Return the ``user_id`` parameter's ``Depends(...)`` default.

    Unwraps the ``@limiter.limit`` decorator first so the real handler
    signature (with the FastAPI dependency default) is inspected.
    """
    target = getattr(func, "__wrapped__", func)
    return inspect.signature(target).parameters["user_id"].default


# ---------------------------------------------------------------------------
# Telegram-reachable LE endpoints: MUST honor X-Owner-User-Id (owner override)
# ---------------------------------------------------------------------------


def test_get_next_review_uses_owner_override_resolver() -> None:
    """GET /api/review/next is reached by the Telegram bot with
    X-Owner-User-Id; it must resolve the owner, not require a session."""
    assert (
        _user_id_dep(review.get_next_review).dependency
        is current_user_id_strict_with_owner_override
    )


def test_submit_review_uses_owner_override_resolver() -> None:
    """POST /api/review/{card_id:int} is reached by the Telegram bot per-user."""
    assert (
        _user_id_dep(review.submit_review).dependency is current_user_id_strict_with_owner_override
    )


def test_get_stats_uses_owner_override_resolver() -> None:
    """GET /api/stats is reached by the Telegram bot (paper_commands,
    daily_briefing, review_reminder) per-user with X-Owner-User-Id."""
    assert _user_id_dep(review.get_stats).dependency is current_user_id_strict_with_owner_override


def test_log_focus_session_uses_owner_override_resolver() -> None:
    """POST /api/executive/focus/log is reached by the Telegram bot
    (system_commands) per-user with X-Owner-User-Id."""
    assert (
        _user_id_dep(executive.log_focus_session).dependency
        is current_user_id_strict_with_owner_override
    )


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


def test_cards_router_is_session_only_by_design() -> None:
    """cards.py is NOT reached by the Telegram bot per-user. It is
    deliberately left on the session-only resolver — converting it would add
    an unused X-Owner-User-Id surface. This guard fails loudly if a future
    change silently widens the auth surface here without a documented caller.
    """
    dep = _user_id_dep(cards.list_cards)
    assert dep.dependency is current_user_id_strict
    assert dep.dependency is not current_user_id_strict_with_owner_override


def test_quick_add_task_is_session_only_by_design() -> None:
    """POST /api/executive/tasks is NOT reached by the Telegram bot per-user.
    No cross-service caller of this route exists (only /api/executive/focus/log
    is Telegram-reached). The owner-override resolver is unnecessary blast
    radius — any shared-API-key holder from an allowlisted net could create
    tasks in any user's account via X-Owner-User-Id. This guard fails loudly
    if a future change silently re-widens the auth surface without a documented
    caller (DA-03).
    """
    dep = _user_id_dep(executive.quick_add_task)
    assert dep.dependency is current_user_id_strict
    assert dep.dependency is not current_user_id_strict_with_owner_override
