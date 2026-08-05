"""Pin the exact route set that honours the X-Owner-User-Id override.

The override lets its caller act as any user, so it is scoped to the routes the
Telegram bot actually calls on a user's behalf. Nothing else in the suite
notices a route quietly opting in — or a bot route quietly opting out and 401ing
in production — so this test asserts the set exactly.

The set is (METHOD, path) tuples, not paths: two bot paths carry a second route
on a different method (`DELETE /api/papers/{paper_id}` is a cascading permanent
delete, `POST /api/digest/weekly` regenerates the digest) and both must stay on
the session-only resolver.
"""

from __future__ import annotations

import httpx
import pytest
from httpx import ASGITransport
from jarvis_common.auth import current_user_id_strict_with_owner_override
from paper_ingestion.main import app

from tests.conftest import _make_pool_and_conn

try:  # FastAPI >=0.137 flattens the route tree through this public iterator.
    from fastapi.routing import (
        iter_route_contexts as _iter_route_contexts,  # type: ignore[attr-defined]
    )
except ImportError:  # FastAPI <0.137 has no iterator; the walk cannot run.
    _iter_route_contexts = None

# Every (method, path) the Telegram bot calls, from its services_client URL
# shapes plus the paper-action suffix table in its callback handler.
_BOT_ROUTE_TUPLES = frozenset(
    {
        ("GET", "/api/papers/feed"),
        ("GET", "/api/papers/{paper_id}"),
        ("GET", "/api/pulse/today"),
        ("GET", "/api/digest/weekly"),
        ("POST", "/api/authors/check"),
        ("POST", "/api/search"),
        ("POST", "/api/papers/{paper_id}/feedback"),
        ("POST", "/api/pulse/generate"),
        ("PUT", "/api/papers/{paper_id}/save"),
        ("PUT", "/api/papers/{paper_id}/skip"),
        ("PUT", "/api/papers/{paper_id}/reading"),
        ("PUT", "/api/papers/{paper_id}/done"),
        ("PUT", "/api/papers/{paper_id}/trash"),
        ("PUT", "/api/papers/{paper_id}/restore"),
        ("PUT", "/api/papers/{paper_id}/trash_and_reject"),
        ("PUT", "/api/papers/{paper_id}/star"),
        ("PUT", "/api/papers/{paper_id}/unstar"),
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
def test_owner_override_is_scoped_to_the_bot_route_set() -> None:
    observed = _observed_owner_override_routes()

    assert observed == set(_BOT_ROUTE_TUPLES), (
        "the X-Owner-User-Id override must reach exactly the Telegram bot's "
        f"route set; unexpectedly honouring {sorted(observed - _BOT_ROUTE_TUPLES)}, "
        f"missing {sorted(set(_BOT_ROUTE_TUPLES) - observed)}"
    )


@pytest.fixture()
def _app_without_api_key_gate():
    """paper_ingestion app with the API-key gate and the limiter out of the way.

    Both answer 401 themselves, so without this the identity assertions below
    would pass no matter which resolver a route uses.
    """
    from jarvis_common import verify_api_key
    from jarvis_common.testing_contract_apps import PITestAppOptions, patch_pi_test_app
    from paper_ingestion.deps import get_db_pool, limiter

    pool, _conn = _make_pool_and_conn()
    # remove_owner_override stays False: this file asserts how routes resolve
    # identity UNDER the autouse stub, so the stub must remain in place.
    with patch_pi_test_app(
        pool,
        app=app,
        get_db_pool=get_db_pool,
        limiter=limiter,
        options=PITestAppOptions(
            remove_owner_override=False,
            override_db_dependency=True,
            disable_limiter=True,
            dependency_overrides={verify_api_key: lambda: None},
        ),
    ):
        yield app


async def test_a_directly_strict_route_still_requires_a_session(_app_without_api_key_gate) -> None:
    """The suite-wide identity stub must not reach a strict route.

    The autouse fixture stubs the two wrapper resolvers; a route that declares
    ``current_user_id_strict`` itself keeps resolving for real, so a test that
    needs an identity there must still say which one. Stubbing the strict
    resolver instead would silently satisfy every cross-user isolation
    assertion in the suite.
    """
    # raise_app_exceptions=False: the positive control resolves an identity and
    # then fails inside the handler on the mock pool, which is still proof that
    # authentication was not what stopped it.
    async with httpx.AsyncClient(
        transport=ASGITransport(app=_app_without_api_key_gate, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        strict = await client.get("/api/pulse/explain/1")
        stubbed = await client.get("/api/pulse/today")

    assert strict.status_code == 401, (
        "a route wired straight to current_user_id_strict must still 401 under "
        f"the autouse identity stub; got {strict.status_code}"
    )
    assert strict.json()["detail"] == "Authentication required", (
        f"the 401 must come from the identity resolver, not from another gate; got {strict.json()}"
    )
    assert stubbed.status_code != 401, (
        "positive control: a route the autouse stub does cover must resolve an "
        f"identity; got {stubbed.status_code} {stubbed.text[:120]}"
    )
