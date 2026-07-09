"""Shared FastAPI health-check route registration.

Both ``paper_ingestion`` and ``learning_engine`` had hand-rolled
``_run_health_checks`` aggregators + ``/health`` + ``/health/internal`` route
handlers that diverged only in their dependency probes.  This module extracts
the aggregator and both route handlers so each service supplies its own list
of probes and nothing else.

Public surface
--------------
* :class:`HealthCheck` -- a ``(name, probe)`` pair where ``probe`` is an
  async callable taking :class:`fastapi.Request` and returning the per-check
  status string (``"ok"`` / ``"unavailable"`` / ``"unknown"``).  Probes are
  expected to swallow their own exceptions and return the status string
  instead; as a fail-safe the aggregator maps a probe that nevertheless
  raises to ``"unavailable"`` (and a per-probe timeout to ``"timeout"``).
* :func:`register_health_routes` -- registers the public ``GET /health``
  (status-only, no auth) and authenticated ``GET /health/internal`` (full
  ``{status, service, checks}``) on the given FastAPI app.

Status semantics
----------------
``"ok"`` and ``"unknown"`` are both treated as non-degraded.  ``"unknown"``
exists for probes whose target may be intentionally disabled (e.g. the
paper_ingestion vector sidecar API) -- the service still surfaces the
status string in ``/health/internal`` for operator visibility without
dragging the overall status to ``"degraded"``.  Any other value is treated
as degraded.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from jarvis_common.auth import verify_api_key
from jarvis_common.maintenance import maintenance_active
from jarvis_common.models import HealthCheckResponse
from jarvis_common.version import app_version

if TYPE_CHECKING:
    import asyncpg
    from slowapi import Limiter

    from jarvis_common.llm_client import LiteLLMConfig

__all__ = [
    "HealthCheck",
    "HealthProbe",
    "make_litellm_probe",
    "make_postgres_probe",
    "register_health_routes",
    "run_health_checks",
]

HealthProbe = Callable[[Request], Awaitable[str]]
HealthCheck = tuple[str, HealthProbe]

# Status values that do NOT contribute to a "degraded" overall status.
_OK_STATUSES: frozenset[str] = frozenset({"ok", "unknown"})

# Per-probe wall-clock budget — keeps the aggregator responsive even when a
# downstream dependency hangs (L-08).
_PROBE_TIMEOUT_S: float = 5.0

# Short-lived memo of the last sweep so the M4.1 frontend poll — which hits
# ``/health`` and ``/health/internal`` back-to-back each cycle — reuses one
# probe run instead of sweeping twice (D3 "double-sweep").  The TTL is well
# under any poll interval, so a degraded/unknown result is never served as
# healthy beyond this window.
_SWEEP_MEMO_TTL_S: float = 1.0
_SWEEP_MEMO_ATTR: str = "_health_sweep_memo"
_SWEEP_TASK_ATTR: str = "_health_sweep_task"

_logger = logging.getLogger(__name__)


def _app_version() -> str:
    """Installed distribution version, surfaced by ``/health/internal``."""
    return app_version()


@dataclass(slots=True)
class _SweepMemo:
    expires_at: float
    status: str
    results: dict[str, str]


async def _execute_sweep(request: Request, checks: list[HealthCheck]) -> tuple[str, dict[str, str]]:
    """Run every probe concurrently and return ``(status, checks_dict)``.

    All probes are awaited together under one 5s ``asyncio.wait_for`` budget
    each (L-08), so the worst-case latency is one ``_PROBE_TIMEOUT_S`` rather
    than the sum across probes. A probe that exceeds the budget is recorded as
    ``"timeout"``; a probe that raises despite the expectation that it swallow
    its own exceptions is recorded as ``"unavailable"`` so the overall response
    is still well-formed.
    """
    outcomes = await asyncio.gather(
        *(asyncio.wait_for(probe(request), timeout=_PROBE_TIMEOUT_S) for _name, probe in checks),
        return_exceptions=True,
    )
    results: dict[str, str] = {}
    for (name, _probe), outcome in zip(checks, outcomes, strict=True):
        if isinstance(outcome, TimeoutError):
            _logger.warning("Health probe %r exceeded %.1fs", name, _PROBE_TIMEOUT_S)
            results[name] = "timeout"
        elif isinstance(outcome, BaseException):
            results[name] = "unavailable"
        else:
            results[name] = outcome

    status = "ok" if all(v in _OK_STATUSES for v in results.values()) else "degraded"
    return status, results


async def run_health_checks(
    request: Request, checks: list[HealthCheck]
) -> tuple[str, dict[str, str]]:
    """Return ``(status, checks_dict)``, reusing in-flight and recent sweeps.

    Probes run concurrently (see :func:`_execute_sweep`). A sweep already in
    progress on ``request.app.state`` is shared by simultaneous ``/health`` and
    ``/health/internal`` requests, then the result is cached for
    :data:`_SWEEP_MEMO_TTL_S`. The TTL is shorter than any poll interval, so a
    degraded/unknown status is never served as healthy past the window.
    """
    now = time.monotonic()
    memo: _SweepMemo | None = getattr(request.app.state, _SWEEP_MEMO_ATTR, None)
    if memo is not None and now < memo.expires_at:
        return memo.status, dict(memo.results)

    task: asyncio.Task[tuple[str, dict[str, str]]] | None = getattr(
        request.app.state, _SWEEP_TASK_ATTR, None
    )
    if task is None or task.done():
        task = asyncio.create_task(_execute_sweep(request, checks))
        setattr(request.app.state, _SWEEP_TASK_ATTR, task)

    try:
        status, results = await task
    finally:
        if getattr(request.app.state, _SWEEP_TASK_ATTR, None) is task and task.done():
            delattr(request.app.state, _SWEEP_TASK_ATTR)

    setattr(
        request.app.state,
        _SWEEP_MEMO_ATTR,
        _SweepMemo(
            expires_at=time.monotonic() + _SWEEP_MEMO_TTL_S,
            status=status,
            results=results,
        ),
    )
    return status, dict(results)


def register_health_routes(
    app: FastAPI,
    *,
    service_name: str,
    checks: list[HealthCheck],
    limiter: Limiter | None = None,
) -> None:
    """Register ``GET /health/live`` + ``/health`` + ``/health/internal``.

    Parameters
    ----------
    app:
        FastAPI application to attach the routes to.
    service_name:
        Used as the ``service`` field in :class:`HealthCheckResponse` from
        ``/health/internal``.
    checks:
        Ordered list of ``(name, probe)`` pairs.  Each probe is an async
        callable receiving ``request`` and returning ``"ok"`` /
        ``"unavailable"`` / ``"unknown"``.  The service owns this list so
        the shared route handler stays domain-agnostic.
    limiter:
        When supplied, every health route is registered with
        ``limiter.exempt`` so the global ``default_limits`` cap enforced by
        ``SlowAPIMiddleware`` never throttles them.  Health checks share one
        unauthenticated ``ip:<addr>`` bucket with all other anonymous traffic
        from the same proxy; without this exemption a monitoring/load-balancer
        poll is starved of quota under sustained load and the orchestrator
        sees spurious 429s.  Omit it to keep the legacy (rate-limited)
        behaviour, e.g. in unit tests that build no limiter.

    Route behaviour
    ---------------
    * ``GET /health/live`` (no auth, no probes) returns ``{"status": "ok"}``
      unconditionally — a process-liveness signal that never blocks on a
      downstream dependency.  Use this for the orchestrator's restart probe;
      use ``/health`` for the load-balancer's readiness probe.
    * ``GET /health`` (no auth) returns only ``{"status": "ok"|"degraded"}``.
      HTTP 200 when status is ``"ok"``, HTTP 503 when ``"degraded"``.  Never
      exposes dependency details to unauthenticated callers.
    * ``GET /health/internal`` requires ``verify_api_key`` and returns the
      full :class:`HealthCheckResponse` body.  Same 200/503 split.

    """

    def _exempt(fn: Any) -> Any:
        return limiter.exempt(fn) if limiter is not None else fn

    @app.get("/health/live", dependencies=[], response_model=None)
    @_exempt
    async def health_live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health", dependencies=[], response_model=None)
    @_exempt
    async def health_check(request: Request) -> dict[str, Any]:
        status, _ = await run_health_checks(request, checks)
        content = {"status": status}
        if status == "degraded":
            return JSONResponse(status_code=503, content=content)  # type: ignore[return-value]
        return content

    @app.get("/health/internal", response_model=HealthCheckResponse)
    @_exempt
    async def health_check_internal(
        request: Request,
        _auth: None = Depends(verify_api_key),
    ) -> HealthCheckResponse:
        status, results = await run_health_checks(request, checks)
        body = HealthCheckResponse(
            status=status,
            service=service_name,
            checks=results,
            maintenance=maintenance_active(),
            version=_app_version(),
        )
        if status == "degraded":
            return JSONResponse(status_code=503, content=body.model_dump())  # type: ignore[return-value]
        return body


# ---------------------------------------------------------------------------
# Shared probe factories (L-10)
#
# paper_ingestion and learning_engine both had near-identical _probe_postgres /
# _probe_litellm functions.  Extracting them here keeps the failure-handling
# (warning log + "unavailable") policy consistent across services and ensures
# a fix to one applies to both.  The factories return :data:`HealthProbe`
# callables suitable for the ``checks`` list passed to
# :func:`register_health_routes`.
# ---------------------------------------------------------------------------


def make_postgres_probe(
    pool: asyncpg.Pool | None = None,
    *,
    state_attr: str = "db_pool",
) -> HealthProbe:
    """Build a ``HealthProbe`` that executes ``SELECT 1`` against an asyncpg pool.

    Two binding modes:

    * Pass *pool* explicitly when it is already constructed at registration
      time (e.g. inside a lifespan callback).
    * Omit *pool* to defer resolution: the returned probe reads
      ``request.app.state.<state_attr>`` per call. This is the common case
      because ``register_health_routes`` runs at module load while the pool
      is created later in ``configure_lifespan``.

    Returns ``"ok"`` when the round-trip succeeds, ``"unavailable"`` (with a
    warning log) on any exception. The per-probe timeout is enforced by
    :func:`run_health_checks` (L-08).
    """

    async def _probe(request: Request) -> str:
        resolved = pool if pool is not None else getattr(request.app.state, state_attr)
        try:
            async with resolved.acquire() as conn:
                await conn.fetchval("SELECT 1")
        except Exception:
            _logger.warning("Health check: PostgreSQL unavailable", exc_info=True)
            return "unavailable"
        return "ok"

    return _probe


def make_litellm_probe(
    http_client: httpx.AsyncClient | None = None,
    config: LiteLLMConfig | None = None,
    *,
    http_client_attr: str = "http_client",
) -> HealthProbe:
    """Build a ``HealthProbe`` that hits LiteLLM's ``/health/readiness``.

    Pass *http_client* and *config* explicitly when both are already available.
    Otherwise the probe first tries ``request.app.state.<http_client_attr>`` so
    it matches service-local wiring, then retries once with a dedicated
    short-lived client if the shared pool is saturated. A health check should
    not report LiteLLM down solely because product LLM traffic has occupied the
    shared app client.

    Returns ``"ok"`` on HTTP 200, ``"unavailable"`` on any other status or
    exception. The per-probe timeout is enforced by :func:`run_health_checks`.
    """

    async def _get_readiness(client: httpx.AsyncClient, base_url: str) -> httpx.Response:
        return await client.get(f"{base_url}/health/readiness", timeout=2.0)

    async def _probe(request: Request) -> str:
        try:
            cfg = config
            if cfg is None:
                from jarvis_common.llm_client import get_litellm_config  # noqa: PLC0415

                cfg = get_litellm_config()

            if http_client is not None:
                resp = await _get_readiness(http_client, cfg.base_url)
            else:
                try:
                    shared_client = getattr(request.app.state, http_client_attr)
                    resp = await _get_readiness(shared_client, cfg.base_url)
                except Exception:
                    _logger.debug(
                        "LiteLLM health check via shared HTTP client failed; "
                        "retrying with dedicated client",
                        exc_info=True,
                    )
                    async with httpx.AsyncClient() as dedicated_client:
                        resp = await _get_readiness(dedicated_client, cfg.base_url)
            return "ok" if resp.status_code == 200 else "unavailable"
        except Exception:
            _logger.warning("Health check: LiteLLM unavailable", exc_info=True)
            return "unavailable"

    return _probe
