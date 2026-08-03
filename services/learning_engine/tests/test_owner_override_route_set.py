"""Pin the exact route set that honours the X-Owner-User-Id override.

The override lets its caller act as any user, so every route that resolves it
is part of the impersonation surface. ``test_rb3_cross_service_auth_boundary``
pins named handlers positively and negatively, but a NEW route adopting the
override would not appear there; this test walks the live route table and
asserts the full set exactly, so any drift — a route quietly opting in, or a
bot-reachable route quietly opting out and 401ing in production — fails here.

The set is (METHOD, path) tuples, not paths: several paths carry sibling
routes on other methods (``PUT``/``DELETE /api/projects/{project_id}``,
``DELETE /api/tasks/{task_id}``) that stay on the session-only resolver by
design.
"""

from __future__ import annotations

import pytest
from jarvis_common.auth import current_user_id_strict_with_owner_override
from learning_engine.main import app

try:  # FastAPI >=0.137 flattens the route tree through this public iterator.
    from fastapi.routing import (
        iter_route_contexts as _iter_route_contexts,  # type: ignore[attr-defined]
    )
except ImportError:  # FastAPI <0.137 has no iterator; the walk cannot run.
    _iter_route_contexts = None

# Every (method, path) that resolves the override today. Twelve are the
# Telegram bot's learning_engine call sites; my-day, my-day-bundle and
# review/sync also declare the resolver and are pinned as they ship.
_OWNER_OVERRIDE_ROUTE_TUPLES = frozenset(
    {
        ("GET", "/api/executive/my-day"),
        ("GET", "/api/executive/my-day-bundle"),
        ("POST", "/api/executive/focus/log"),
        ("GET", "/api/projects"),
        ("POST", "/api/projects"),
        ("GET", "/api/projects/{project_id}"),
        ("GET", "/api/projects/{project_id}/tasks"),
        ("GET", "/api/tasks"),
        ("PUT", "/api/tasks/{task_id}"),
        ("GET", "/api/projects/{project_id}/milestones"),
        ("GET", "/api/milestones/upcoming"),
        ("GET", "/api/review/next"),
        ("POST", "/api/review/{card_id:int}"),
        ("POST", "/api/review/sync"),
        ("GET", "/api/stats"),
    }
)

_IMPLICIT_METHODS = frozenset({"HEAD", "OPTIONS"})


def _dependency_calls(dependant: object) -> set[object]:
    """Every callable in *dependant*'s tree, including sub-dependants.

    Routes bind the identity under several parameter names, so the parameter
    signature cannot answer which resolver a route uses — the dependency tree
    can.
    """
    calls: set[object] = set()
    pending = [dependant]
    while pending:
        current = pending.pop()
        calls.add(current.call)  # type: ignore[attr-defined]
        pending.extend(current.dependencies)  # type: ignore[attr-defined]
    return calls


def _observed_owner_override_routes() -> set[tuple[str, str]]:
    assert _iter_route_contexts is not None
    observed: set[tuple[str, str]] = set()
    for context in _iter_route_contexts(app.routes):
        dependant = getattr(context.route, "dependant", None)
        if dependant is None:
            continue
        if current_user_id_strict_with_owner_override not in _dependency_calls(dependant):
            continue
        for method in context.methods or ():
            if method not in _IMPLICIT_METHODS:
                observed.add((method, context.path))
    return observed


@pytest.mark.skipif(
    _iter_route_contexts is None,
    reason="fastapi.routing.iter_route_contexts is absent below FastAPI 0.137, "
    "so the route-table walk this pin needs cannot run",
)
def test_owner_override_is_scoped_to_the_pinned_route_set() -> None:
    observed = _observed_owner_override_routes()

    assert observed == set(_OWNER_OVERRIDE_ROUTE_TUPLES), (
        "the X-Owner-User-Id override must reach exactly the pinned route set; "
        f"unexpectedly honouring {sorted(observed - _OWNER_OVERRIDE_ROUTE_TUPLES)}, "
        f"missing {sorted(set(_OWNER_OVERRIDE_ROUTE_TUPLES) - observed)}"
    )
