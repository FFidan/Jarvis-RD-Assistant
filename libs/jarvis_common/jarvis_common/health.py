"""Shared FastAPI health-check route registration.

DOM-J-03: Both ``paper_ingestion`` and ``learning_engine`` had hand-rolled
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
  instead; the aggregator does NOT translate exceptions to ``"unavailable"``.
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
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from jarvis_common.auth import verify_api_key
from jarvis_common.models import HealthCheckResponse

if TYPE_CHECKING:
    import asyncpg
    import httpx
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

_logger = logging.getLogger(__name__)


async def run_health_checks(
    request: Request, checks: list[HealthCheck]
) -> tuple[str, dict[str, str]]:
    """Execute every probe sequentially and return ``(status, checks_dict)``.

    Each probe is awaited in declared order under a 5s ``asyncio.wait_for``
    budget (L-08); a probe that exceeds the budget is recorded as
    ``"timeout"`` so a hung dependency cannot stall the ``/health`` endpoint
    indefinitely. Probes are expected to handle their own exceptions and
    return a status string; if a probe nevertheless raises, the aggregator
    records the check as ``"unavailable"`` so the overall response is still
    well-formed.
    """
    results: dict[str, str] = {}
    for name, probe in checks:
        try:
            results[name] = await asyncio.wait_for(probe(request), timeout=_PROBE_TIMEOUT_S)
        except TimeoutError:
            _logger.warning("Health probe %r exceeded %.1fs", name, _PROBE_TIMEOUT_S)
            results[name] = "timeout"
        except Exception:  # Best-effort: a misbehaving probe must not 500 the endpoint
            results[name] = "unavailable"

    status = "ok" if all(v in _OK_STATUSES for v in results.values()) else "degraded"
    return status, results


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
      exposes dependency details to unauthenticated callers (SEC-H09).
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
        body = HealthCheckResponse(status=status, service=service_name, checks=results)
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

    Pass *http_client* and *config* explicitly when both are already
    available; otherwise leave them ``None`` and the probe resolves
    ``request.app.state.<http_client_attr>`` plus a fresh
    :func:`get_litellm_config` on each call (matching the pattern of the
    service-local probes this factory replaces).

    Returns ``"ok"`` on HTTP 200, ``"unavailable"`` on any other status or
    exception. The per-probe timeout is enforced by :func:`run_health_checks`.
    """

    async def _probe(request: Request) -> str:
        try:
            client = (
                http_client
                if http_client is not None
                else getattr(request.app.state, http_client_attr)
            )
            cfg = config
            if cfg is None:
                from jarvis_common.llm_client import get_litellm_config  # noqa: PLC0415

                cfg = get_litellm_config()
            resp = await client.get(f"{cfg.base_url}/health/readiness")
            return "ok" if resp.status_code == 200 else "unavailable"
        except Exception:
            _logger.warning("Health check: LiteLLM unavailable", exc_info=True)
            return "unavailable"

    return _probe
