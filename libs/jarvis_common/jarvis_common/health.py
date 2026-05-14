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

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from jarvis_common.auth import verify_api_key
from jarvis_common.models import HealthCheckResponse

__all__ = [
    "HealthCheck",
    "HealthProbe",
    "register_health_routes",
    "run_health_checks",
]

HealthProbe = Callable[[Request], Awaitable[str]]
HealthCheck = tuple[str, HealthProbe]

# Status values that do NOT contribute to a "degraded" overall status.
_OK_STATUSES: frozenset[str] = frozenset({"ok", "unknown"})


async def run_health_checks(
    request: Request, checks: list[HealthCheck]
) -> tuple[str, dict[str, str]]:
    """Execute every probe sequentially and return ``(status, checks_dict)``.

    Each probe is awaited in declared order.  Probes are expected to handle
    their own exceptions and return a status string; if a probe nevertheless
    raises, the aggregator records the check as ``"unavailable"`` so the
    overall response is still well-formed.
    """
    results: dict[str, str] = {}
    for name, probe in checks:
        try:
            results[name] = await probe(request)
        except Exception:  # Best-effort: a misbehaving probe must not 500 the endpoint
            results[name] = "unavailable"

    status = "ok" if all(v in _OK_STATUSES for v in results.values()) else "degraded"
    return status, results


def register_health_routes(
    app: FastAPI,
    *,
    service_name: str,
    checks: list[HealthCheck],
) -> None:
    """Register ``GET /health`` + ``GET /health/internal`` on the FastAPI app.

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

    Route behaviour
    ---------------
    * ``GET /health`` (no auth) returns only ``{"status": "ok"|"degraded"}``.
      HTTP 200 when status is ``"ok"``, HTTP 503 when ``"degraded"``.  Never
      exposes dependency details to unauthenticated callers (SEC-H09).
    * ``GET /health/internal`` requires ``verify_api_key`` and returns the
      full :class:`HealthCheckResponse` body.  Same 200/503 split.
    """

    @app.get("/health", dependencies=[], response_model=None)
    async def health_check(request: Request) -> dict[str, Any]:
        status, _ = await run_health_checks(request, checks)
        content = {"status": status}
        if status == "degraded":
            return JSONResponse(status_code=503, content=content)  # type: ignore[return-value]
        return content

    @app.get("/health/internal", response_model=HealthCheckResponse)
    async def health_check_internal(
        request: Request,
        _auth: None = Depends(verify_api_key),
    ) -> HealthCheckResponse:
        status, results = await run_health_checks(request, checks)
        body = HealthCheckResponse(status=status, service=service_name, checks=results)
        if status == "degraded":
            return JSONResponse(status_code=503, content=body.model_dump())  # type: ignore[return-value]
        return body
