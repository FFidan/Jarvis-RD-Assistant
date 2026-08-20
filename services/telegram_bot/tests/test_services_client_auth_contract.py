"""Static contract for both halves of a Telegram backend call.

The Telegram bot authenticates cross-service calls with X-Jarvis-Paired-User-Id + the
shared API key — it has no browser session. Two things must therefore hold for
every backend route the bot can issue, and each is checked here.

Destination side: each endpoint the bot's services_client targets MUST resolve
the caller via an override-capable dependency. Walks the real route dependants
in both service apps and asserts the override resolver is present and the
session-only resolver is absent.

Caller side: the route must be granted to the Telegram service principal, or
the bot refuses the request before transport and the user sees a dead button.
That set is not hand-maintained — it is read from the client source and the
handler dispatch table, so a new call cannot ship without a matching grant.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from fastapi.routing import APIRoute

try:  # FastAPI >=0.137 flattens the route tree through this public iterator.
    from fastapi.routing import (
        iter_route_contexts as _iter_route_contexts,  # type: ignore[attr-defined]
    )
except ImportError:  # FastAPI <0.137 keeps app.routes a flat APIRoute list.
    _iter_route_contexts = None

from jarvis_common.auth import current_user_id_strict
from jarvis_common.identity_capabilities import service_principal_scopes
from telegram_bot import services_client
from telegram_bot.handlers.callback_handler import _PAPER_ACTION_ENDPOINTS

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
    ("GET", "/api/executive/my-day"),
    ("GET", "/api/executive/focus/active"),
    ("GET", "/api/executive/focus/telegram/pending"),
    ("POST", "/api/executive/focus/start"),
    ("POST", "/api/executive/focus/{session_id}/pause"),
    ("POST", "/api/executive/focus/{session_id}/resume"),
    ("POST", "/api/executive/focus/{session_id}/complete"),
    ("POST", "/api/executive/focus/{session_id}/telegram-notified"),
]
_PI_TARGETS = [
    ("GET", "/api/papers/feed"),
    ("POST", "/api/authors/check"),
    ("POST", "/api/authors/alerts/ack"),
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
    ("GET", "/api/pulse/generate/{job_id}"),
    ("GET", "/api/digest/weekly"),
]

# The two Learning routes the bot calls as a service rather than as a paired
# owner. They are guarded by ``require_telegram_principal`` and never reach
# ``current_user_id_strict``, so they belong to the capability contract below
# and not to the identity-resolver lists above.
_LE_SERVICE_PRINCIPAL_TARGETS = [
    ("GET", "/internal/telegram/nudges"),
    ("POST", "/internal/telegram/nudges/{nudge_id}/ack"),
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


@pytest.mark.parametrize("method,path", _LE_SERVICE_PRINCIPAL_TARGETS)
def test_service_principal_target_requires_the_telegram_principal(method: str, path: str) -> None:
    """Bot-only Learning routes are gated on the verified principal, not a session."""
    from learning_engine.main import app as le_app
    from learning_engine.routers.internal_telegram import require_telegram_principal

    calls = _dependency_calls(_effective_dependant_for(le_app, method, path))
    assert require_telegram_principal in calls, (
        f"{method} {path} does not require the verified Telegram principal"
    )


# ---------------------------------------------------------------------------
# Caller side: the Telegram principal's grant for every route the bot can issue
# ---------------------------------------------------------------------------

#: ``BotConfig`` origin attribute -> signed-assertion audience. ``services_client``
#: is the only module that talks to Learning and Research; Platform calls go
#: through ``platform_client`` and carry no service assertion.
_AUDIENCE_BY_ORIGIN = {
    "learning_engine_url": "learning",
    "paper_ingestion_url": "research",
}

#: HTTP verbs ``services_client`` names directly on its injected client.
_FIXED_VERB_ATTRS = frozenset({"get", "post", "put", "patch", "delete"})

#: The one client call whose verb and path suffix come from its caller:
#: ``update_paper_action`` does not enumerate the paper lifecycle routes, so
#: they are expanded from the handler dispatch table instead.
_CALLER_SUPPLIED_ROUTE = ("research", "/api/papers/{paper_id}/{suffix}")

#: One realistic value per interpolated path parameter. ``job_id`` is a Pulse
#: job identifier, not an integer: a numeric sample would satisfy the manifest's
#: generic segment without resembling anything the client actually sends.
_PATH_PARAM_SAMPLES = {
    "project_id": "17",
    "task_id": "17",
    "card_id": "17",
    "nudge_id": "17",
    "session_id": "17",
    "paper_id": "17",
    "job_id": "b7d9b0f4-1c3a-4f5e-9a2d-6c8e0f31a742",
}


def _path_parameter(node: ast.expr) -> str:
    """Parameter name behind one interpolated path placeholder."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Call) and node.args:  # e.g. quote(job_id, safe="")
        return _path_parameter(node.args[0])
    raise AssertionError(f"unrecognised path placeholder: {ast.unparse(node)}")


def _backend_url(node: ast.expr) -> tuple[str, str]:
    """Audience and path template behind one backend URL literal."""
    assert isinstance(node, ast.JoinedStr), f"backend URL is not a literal: {ast.unparse(node)}"
    origin, *tail = node.values
    assert isinstance(origin, ast.FormattedValue) and isinstance(origin.value, ast.Attribute), (
        f"backend URL does not start with a configured origin: {ast.unparse(node)}"
    )
    audience = _AUDIENCE_BY_ORIGIN.get(origin.value.attr)
    assert audience is not None, f"unmapped backend origin {origin.value.attr!r}"
    template = "".join(
        part.value if isinstance(part, ast.Constant) else f"{{{_path_parameter(part.value)}}}"
        for part in tail
    )
    return audience, template


def _client_routes() -> tuple[tuple[str, str, str], ...]:
    """Every backend route the bot can issue, read from its two sources of truth."""
    module = ast.parse(Path(services_client.__file__).read_text(encoding="utf-8"))
    routes: set[tuple[str, str, str]] = set()
    caller_supplied: set[tuple[str, str]] = set()
    for call in ast.walk(module):
        if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
            continue
        client = call.func.value
        if not (isinstance(client, ast.Name) and client.id == "http"):
            continue
        verb = call.func.attr
        if verb in _FIXED_VERB_ATTRS:
            audience, template = _backend_url(call.args[0])
            routes.add((audience, verb.upper(), template))
        elif verb == "request":
            audience, template = _backend_url(call.args[1])
            caller_supplied.add((audience, template))
            for method, suffix in _PAPER_ACTION_ENDPOINTS.values():
                routes.add((audience, method, template.replace("{suffix}", suffix)))
        else:
            raise AssertionError(f"unclassified backend call http.{verb}")
    assert caller_supplied == {_CALLER_SUPPLIED_ROUTE}, (
        "only the paper lifecycle call may take its verb and path suffix from a "
        f"caller; found {sorted(caller_supplied)}"
    )
    return tuple(sorted(routes))


def _concrete_path(template: str) -> str:
    """The template with one concrete value substituted per path parameter."""

    def sample(match: re.Match[str]) -> str:
        name = match.group(1)
        assert name in _PATH_PARAM_SAMPLES, f"no concrete sample for path parameter {name!r}"
        return _PATH_PARAM_SAMPLES[name]

    return re.sub(r"\{([^}]+)\}", sample, template)


_CLIENT_ROUTES = _client_routes()


@pytest.mark.parametrize("audience,method,template", _CLIENT_ROUTES)
def test_client_route_is_granted_to_the_telegram_principal(
    audience: str, method: str, template: str
) -> None:
    """Every backend route the bot can issue is allowlisted for its principal."""
    path = _concrete_path(template)
    assert service_principal_scopes("telegram", audience, method, path) is not None, (
        f"{method} {path} is not granted to the Telegram service principal, so the "
        "bot refuses the call before it reaches the backend"
    )


def test_client_route_set_covers_both_backends_and_every_paper_action() -> None:
    """The resolved route set really did read both sources of truth."""
    assert {audience for audience, _, _ in _CLIENT_ROUTES} == {"learning", "research"}
    for method, suffix in _PAPER_ACTION_ENDPOINTS.values():
        assert ("research", method, f"/api/papers/{{paper_id}}/{suffix}") in _CLIENT_ROUTES, (
            f"paper action {suffix!r} is missing from the resolved client route set"
        )


def test_routes_the_bot_no_longer_calls_are_not_granted() -> None:
    """A removed client call leaves no standing capability behind."""
    assert service_principal_scopes("telegram", "research", "PUT", "/api/papers/17/unsave") is None
    assert (
        service_principal_scopes("telegram", "learning", "POST", "/api/executive/focus/log") is None
    )
