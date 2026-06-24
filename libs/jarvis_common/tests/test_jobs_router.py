"""Pure-function unit tests for ``jarvis_common.jobs_router.collect_handlers``.

``collect_handlers`` has two code paths gated on the FastAPI version:

* **>=0.137** — ``router.routes`` is a tree, so it flattens it through the public
  ``fastapi.routing.iter_route_contexts`` iterator (module-level
  ``_iter_route_contexts``), reading ``context.route`` for each yielded context.
* **<0.137** — ``iter_route_contexts`` is absent (``_iter_route_contexts is None``),
  ``router.routes`` is already a flat ``APIRoute`` list, so it iterates that directly.

The live deployment runs FastAPI 0.136.3, which exercises ONLY the fallback path.
These tests assert BOTH paths yield the identical full handler-name set, guarding
against the fallback silently returning an empty dict (which would KeyError at the
service-router import site on an older / rolled-back FastAPI).

Verified: jobs_router.py:74-96 — collect_handlers; both branches guard
``isinstance(route, APIRoute)`` + a present ``endpoint`` and key on ``endpoint.__name__``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import asyncpg
from jarvis_common.jobs_router import build_jobs_router, collect_handlers

# Endpoint ``__name__``s defined by build_jobs_router (jobs_router.py:212-368).
EXPECTED_HANDLERS = frozenset({"create_job", "get_job", "list_jobs", "stream_job", "cancel_job"})


def _identity_limiter() -> MagicMock:
    """A SlowAPI ``Limiter`` stub whose ``.limit(spec)`` is an identity decorator."""
    limiter = MagicMock()
    limiter.enabled = False
    limiter.limit = lambda _spec: lambda f: f
    return limiter


def _build_real_router():
    """Build a real ``/api/jobs`` router via the production factory (permissive mode)."""
    return build_jobs_router(
        service_name="unit_test",
        public_kinds=frozenset({"noop.test"}),
        get_db_pool=lambda: MagicMock(spec=asyncpg.Pool),
        limiter=_identity_limiter(),
    )


def _fake_iter_route_contexts(routes):
    """Mimic ``fastapi.routing.iter_route_contexts``: yield a context per route.

    The real iterator flattens the >=0.137 route tree and yields one context per
    effective route, exposing the ``APIRoute`` on ``context.route`` — the only
    attribute ``collect_handlers`` reads (jobs_router.py:86-89). For a flat
    ``router.routes`` list (what the factory produces in either FastAPI era) the
    1:1 wrapping reproduces that contract exactly.
    """
    for route in routes:
        ctx = MagicMock()
        ctx.route = route
        yield ctx


def test_collect_handlers_fallback_branch_returns_full_set(monkeypatch):
    """<0.137 fallback (``_iter_route_contexts is None``) yields all handlers.

    This is the branch the live FastAPI 0.136.3 deployment actually runs. A
    regression that left ``router.routes`` un-iterated would return an empty dict
    here and KeyError at the service jobs.py import.
    """
    router = _build_real_router()
    monkeypatch.setattr("jarvis_common.jobs_router._iter_route_contexts", None)

    handlers = collect_handlers(router)

    assert set(handlers) == EXPECTED_HANDLERS, (
        f"fallback (<0.137) path returned {sorted(handlers)}; expected {sorted(EXPECTED_HANDLERS)}"
    )
    # Each value is the actual endpoint callable keyed by its __name__.
    for name, endpoint in handlers.items():
        assert endpoint.__name__ == name


def test_collect_handlers_iterator_branch_returns_full_set(monkeypatch):
    """>=0.137 path (``_iter_route_contexts`` present) yields all handlers.

    The live env has ``_iter_route_contexts is None`` (FastAPI 0.136.3), so we
    install a faithful stand-in to force this branch and prove it is reachable
    and complete — i.e. it survives a FastAPI >=0.137 upgrade.
    """
    router = _build_real_router()
    monkeypatch.setattr(
        "jarvis_common.jobs_router._iter_route_contexts",
        _fake_iter_route_contexts,
    )

    handlers = collect_handlers(router)

    assert set(handlers) == EXPECTED_HANDLERS, (
        f">=0.137 iterator path returned {sorted(handlers)}; expected {sorted(EXPECTED_HANDLERS)}"
    )
    for name, endpoint in handlers.items():
        assert endpoint.__name__ == name


def test_collect_handlers_both_branches_agree(monkeypatch):
    """Both branches return the IDENTICAL handler-name set for the same router.

    Guards the invariant that the version fallback is behaviour-preserving: the
    <0.137 ``router.routes`` iteration must not silently diverge from the
    >=0.137 ``iter_route_contexts`` flattening.
    """
    router = _build_real_router()

    monkeypatch.setattr("jarvis_common.jobs_router._iter_route_contexts", None)
    fallback = set(collect_handlers(router))

    monkeypatch.setattr(
        "jarvis_common.jobs_router._iter_route_contexts",
        _fake_iter_route_contexts,
    )
    iterator = set(collect_handlers(router))

    assert fallback == iterator == EXPECTED_HANDLERS, (
        f"branch divergence: fallback={sorted(fallback)} iterator={sorted(iterator)}"
    )
