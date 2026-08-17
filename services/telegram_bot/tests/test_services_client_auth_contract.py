"""Static contract: every services_client target endpoint resolves identity via
the owner-override dependency (never session-only).

The Telegram bot authenticates cross-service calls with X-Jarvis-Paired-User-Id + the
shared API key — it has no browser session. So each endpoint the bot's
services_client targets MUST resolve the caller via an override-capable
dependency. Walks the real route dependants in both service apps and asserts
the override resolver is present and the session-only resolver is absent.
"""

from __future__ import annotations

import pytest
from fastapi.routing import APIRoute

try:  # FastAPI >=0.137 flattens the route tree through this public iterator.
    from fastapi.routing import (
        iter_route_contexts as _iter_route_contexts,  # type: ignore[attr-defined]
    )
except ImportError:  # FastAPI <0.137 keeps app.routes a flat APIRoute list.
    _iter_route_contexts = None

from jarvis_common.auth import current_user_id_strict

_IDENTITY_RESOLVERS = {current_user_id_strict}

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
    ("GET", "/api/review/next"),
    ("POST", "/api/review/{card_id:int}"),
    ("POST", "/api/executive/focus/log"),
]
_PI_TARGETS = [
    ("GET", "/api/papers/feed"),
    ("POST", "/api/authors/check"),
    ("GET", "/api/papers/{paper_id}"),
    ("PUT", "/api/papers/{paper_id}/save"),
    ("PUT", "/api/papers/{paper_id}/skip"),
    ("PUT", "/api/papers/{paper_id}/reading"),
    ("PUT", "/api/papers/{paper_id}/done"),
    ("PUT", "/api/papers/{paper_id}/trash"),
    ("PUT", "/api/papers/{paper_id}/restore"),
    ("PUT", "/api/papers/{paper_id}/trash_and_reject"),
    ("PUT", "/api/papers/{paper_id}/star"),
    ("PUT", "/api/papers/{paper_id}/unstar"),
    ("POST", "/api/papers/{paper_id}/feedback"),
    ("POST", "/api/search"),
    ("GET", "/api/pulse/today"),
    ("POST", "/api/pulse/generate"),
    ("GET", "/api/digest/weekly"),
]


def _dependency_calls(dependant) -> set:
    """Flatten every callable in a route's dependant tree (handles nested Depends)."""
    calls: set = set()
    for sub in dependant.dependencies:
        if sub.call is not None:
            calls.add(sub.call)
        calls |= _dependency_calls(sub)
    return calls


def _effective_dependant_for(app, method: str, path: str):
    """The dependant actually resolved for a request, including router/app-level deps.

    ``context.dependant`` is the effective tree — an endpoint's own ``Depends`` plus
    anything attached via ``include_router(dependencies=...)`` or on the app itself.
    ``context.route.dependant`` would only carry the endpoint's own, so a session-only
    resolver bound one layer up would 401 the bot while the contract still looked green.
    """
    if _iter_route_contexts is not None:
        for context in _iter_route_contexts(app.routes):
            if (
                isinstance(context.route, APIRoute)
                and context.path == path
                and method in context.methods
            ):
                return context.dependant
    else:
        for route in app.routes:
            if isinstance(route, APIRoute) and route.path == path and method in route.methods:
                return route.dependant
    raise AssertionError(f"No {method} {path} route found in {app.title!r}")


def _app_targets():
    from learning_engine.main import app as le_app
    from paper_ingestion.main import app as pi_app

    return [(le_app, _LE_TARGETS), (pi_app, _PI_TARGETS)]


@pytest.mark.parametrize(
    "method,path",
    [(m, p) for _, targets in [(None, _LE_TARGETS), (None, _PI_TARGETS)] for m, p in targets],
)
def test_bot_target_endpoint_requires_signed_identity_state(method: str, path: str) -> None:
    """Each bot target resolves identity only from verified assertion state."""
    for app, targets in _app_targets():
        if (method, path) not in targets:
            continue
        calls = _dependency_calls(_effective_dependant_for(app, method, path))
        assert calls & _IDENTITY_RESOLVERS, (
            f"{method} {path} does not resolve identity through the strict state seam"
        )
        return
    raise AssertionError(f"{method} {path} not mapped to an app")
