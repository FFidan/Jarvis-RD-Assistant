"""Static contract: every services_client target endpoint resolves identity via
the owner-override dependency (never session-only).

The Telegram bot authenticates cross-service calls with X-Owner-User-Id + the
shared API key — it has no browser session. So each endpoint the bot's
services_client targets MUST resolve the caller via an override-capable
dependency. Walks the real route dependants in both service apps and asserts
the override resolver is present and the session-only resolver is absent.
"""

from __future__ import annotations

import pytest
from fastapi.routing import APIRoute
from jarvis_common.auth import (
    current_user_id_strict,
    current_user_id_strict_with_owner_override,
    current_user_id_with_owner_override,
)

_OVERRIDE_RESOLVERS = {
    current_user_id_with_owner_override,
    current_user_id_strict_with_owner_override,
}

# (method, full path template) the bot's services_client targets.
# Adding a services_client call REQUIRES adding its endpoint here.
_LE_TARGETS = [
    ("GET", "/api/projects"),
    ("POST", "/api/projects"),
    ("GET", "/api/projects/{project_id}"),
    ("GET", "/api/projects/{project_id}/tasks"),
    ("GET", "/api/projects/{project_id}/milestones"),
    ("GET", "/api/tasks"),
    ("PUT", "/api/tasks/{task_id}"),
    ("GET", "/api/milestones/upcoming"),
    ("GET", "/api/stats"),
]
_PI_TARGETS = [
    ("GET", "/api/papers/feed"),
    ("POST", "/api/authors/check"),
]


def _dependency_calls(dependant) -> set:
    """Flatten every callable in a route's dependant tree (handles nested Depends)."""
    calls: set = set()
    for sub in dependant.dependencies:
        if sub.call is not None:
            calls.add(sub.call)
        calls |= _dependency_calls(sub)
    return calls


def _route_for(app, method: str, path: str) -> APIRoute:
    for route in app.routes:
        if isinstance(route, APIRoute) and route.path == path and method in route.methods:
            return route
    raise AssertionError(f"No {method} {path} route found in {app.title!r}")


def _app_targets():
    from learning_engine.main import app as le_app
    from paper_ingestion.main import app as pi_app

    return [(le_app, _LE_TARGETS), (pi_app, _PI_TARGETS)]


@pytest.mark.parametrize(
    "method,path",
    [(m, p) for _, targets in [(None, _LE_TARGETS), (None, _PI_TARGETS)] for m, p in targets],
)
def test_bot_target_endpoint_uses_owner_override(method: str, path: str) -> None:
    """Each bot-target endpoint must bind an owner-override resolver, never session-only."""
    for app, targets in _app_targets():
        if (method, path) not in targets:
            continue
        route = _route_for(app, method, path)
        calls = _dependency_calls(route.dependant)
        assert calls & _OVERRIDE_RESOLVERS, (
            f"{method} {path} does not resolve identity via an owner-override dep "
            f"— the Telegram bot (X-Owner-User-Id, no session) would 401"
        )
        assert current_user_id_strict not in calls, (
            f"{method} {path} resolves via session-only current_user_id_strict — "
            f"the bot has no browser session and would 401"
        )
        return
    raise AssertionError(f"{method} {path} not mapped to an app")
