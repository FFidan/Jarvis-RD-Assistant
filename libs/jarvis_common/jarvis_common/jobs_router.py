"""Factory for service-specific ``/api/jobs`` REST routers.

Both ``paper_ingestion`` and ``learning_engine`` exposed ~95% byte-identical
``routers/jobs.py`` modules (enqueue + status + list + SSE stream + cancel).
This module collapses the shared structure into a single factory.

Per-service differences are encapsulated as factory parameters:

* ``service_name``    — used in log messages / error context.
* ``public_kinds``    — the base allowlist of kinds clients may enqueue.
* ``payload_schemas`` — optional ``{kind: BaseModel}`` mapping. When provided,
  the request body is validated through a Pydantic discriminated union
  (PI-style strict mode → unknown / mis-shaped payloads return ``422``).
  When ``None``, the body is accepted as a bare ``dict`` (LE-style permissive
  mode → unknown kinds return ``400`` from the allowlist guard).

Behavioural contracts preserved (do not change without updating both
service test suites):

* ``LE-002`` — unknown kinds in permissive mode return ``400`` (not ``422``).
* ``SYM-002`` — ``CreateJobRequest.payload`` uses
  ``Field(default_factory=dict)``; the default is never a shared mutable.
* ``LE-002`` — ownership comparisons coerce both sides to ``str``
  so that asyncpg-returned ``user_id='42'`` matches caller ``user_id=42``.
* ``noop.test`` — appended to the allowlist when
  ``get_jobs_settings().test_jobs_enabled`` is true (re-evaluated on every
  request so test-only env toggles work without a restart).

NOTE: this module deliberately does **not** use
``from __future__ import annotations``.  The endpoints are built as closures
whose ``body: CreateJobRequest`` annotation references a dynamically-built
model class held in a local variable.  PEP-563 string annotations would
force FastAPI's ``get_type_hints`` to resolve ``CreateJobRequest`` against
module globals (where it does not exist), so the body would silently be
treated as a query parameter — producing ``{"loc": ["query", "body"]}``
validation errors.  Keeping annotations evaluated at runtime makes
introspection work correctly.
"""

from collections.abc import Callable, Mapping
from typing import Annotated, Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field, TypeAdapter, field_validator, model_validator
from slowapi import Limiter

try:  # FastAPI >=0.137 flattens the route tree through this public iterator.
    from fastapi.routing import (
        iter_route_contexts as _iter_route_contexts,  # type: ignore[attr-defined]
    )
except ImportError:  # FastAPI <0.137 keeps router.routes a flat APIRoute list.
    _iter_route_contexts = None

from jarvis_common import (
    ErrorResponse,
    JobCreateResponse,
    JobStatusResponse,
    assert_paper_ownership,
    assert_papers_ownership,
    current_user_id_strict,
)
from jarvis_common import jobs as jobs_lib
from jarvis_common.settings import get_jobs_settings

__all__ = ["build_jobs_router", "collect_handlers", "serialise_row"]


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def collect_handlers(router: APIRouter) -> dict[str, Callable[..., Any]]:
    """Map ``endpoint.__name__`` → endpoint for every ``APIRoute`` in ``router``.

    FastAPI >=0.137 turned ``router.routes`` into a tree (``APIRoute`` plus
    opaque ``_IncludedRouter`` nodes), so a flat comprehension misses routes from
    included sub-routers; its public ``iter_route_contexts`` flattens that tree.
    On <0.137 the iterator is absent and ``router.routes`` is already flat, so we
    fall back to iterating it directly. Both paths guard ``isinstance(APIRoute)``
    + a present ``endpoint`` so non-route nodes are skipped.
    """
    handlers: dict[str, Callable[..., Any]] = {}
    if _iter_route_contexts is not None:
        for context in _iter_route_contexts(router.routes):
            route = context.route
            endpoint = getattr(route, "endpoint", None)
            if isinstance(route, APIRoute) and endpoint is not None:
                handlers[endpoint.__name__] = endpoint
    else:
        for route in router.routes:
            endpoint = getattr(route, "endpoint", None)
            if isinstance(route, APIRoute) and endpoint is not None:
                handlers[endpoint.__name__] = endpoint
    return handlers


def serialise_row(row: dict[str, Any]) -> dict[str, Any]:
    """Convert asyncpg row values (UUIDs, datetimes) to JSON-safe types."""
    out: dict[str, Any] = {}
    for k, v in row.items():
        if hasattr(v, "isoformat"):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out


def _owner_matches(row_user_id: Any, caller_user_id: int | None) -> bool:
    """Ownership check that tolerates str/int mismatches between DB row + caller.

    asyncpg may return ``user_id`` as ``str`` (UUID-shaped column) or ``int``
    (legacy schemas). Coercing both sides to ``str`` keeps the comparison
    correct in either case (LE-002 fix preserved).

    NULL-row jobs are system-only and require an authenticated caller.
    Anonymous callers (caller_user_id is None) are always rejected — this
    closes the SSE auth bypass where NULL-row jobs matched unauthenticated
    requests (SEC-CRIT-01 / H-05).
    """
    if caller_user_id is None:
        return False
    if row_user_id is None:
        # System-only job: authenticated callers may reach here (above guard
        # already filtered None), but system rows are not user-owned.
        return False
    return str(row_user_id) == str(caller_user_id)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_jobs_router(
    *,
    service_name: str = "",
    public_kinds: frozenset[str],
    get_db_pool: Callable[..., asyncpg.Pool],
    limiter: Limiter,
    payload_schemas: dict[str, type[BaseModel]] | None = None,
    paper_ownership_extractor: Callable[[dict[str, Any]], int | list[int] | None] | None = None,
    task_lookup: Callable[[], Mapping[str, Any]] | None = None,
) -> APIRouter:
    """Build a ``/api/jobs`` router wired to the given service's deps.

    Parameters
    ----------
    service_name:
        Optional service identifier (e.g. ``"paper_ingestion"``).  Currently
        unused; accepted for backward compatibility.
    public_kinds:
        Base allowlist of job kinds clients may enqueue via ``POST /api/jobs``.
        ``noop.test`` is added at request-time when test jobs are enabled.
    get_db_pool:
        FastAPI dependency callable returning the service's asyncpg pool.
    limiter:
        Service-local SlowAPI ``Limiter`` (shared instance from ``deps.py``).
    payload_schemas:
        Optional ``{kind: BaseModel}`` map.  When provided the factory builds
        a Pydantic discriminated union so per-kind shape errors are caught
        at parse time (HTTP 422).  When ``None`` (or empty), the request
        accepts ``payload: dict[str, Any]`` and only the allowlist is
        enforced (HTTP 400 for unknown kinds — see LE-002).
    paper_ownership_extractor:
        Optional ``payload -> paper_id | None`` callable.  When provided AND it
        returns an ``int``, ``create_job`` calls
        :func:`jarvis_common.db_helpers.assert_paper_ownership` before enqueue
        so users cannot enqueue paper-scoped jobs against papers they do not
        own.  ``create_job`` requires a resolved user identity
        (``current_user_id_strict``), so ``user_id=None`` is never reached via
        the public API.  ``None`` means the service has no paper-scoped
        jobs (e.g. ``learning_engine`` — which wires its own extractor).
    task_lookup:
        Optional callable returning the current kind→task mapping.  Defaults to
        the compatibility ``jarvis_common.task_registry.KIND_TO_TASK`` mapping.

    """
    # ``service_name`` is currently informational but kept on the closure so
    # future audit/log integrations can read it without adding a parameter.
    _ = service_name

    def _public_kinds_now() -> set[str]:
        kinds = set(public_kinds)
        if get_jobs_settings().test_jobs_enabled:
            kinds.add("noop.test")
        return kinds

    # ------------------------------------------------------------------
    # Build the request model — strict (discriminated) or permissive
    # ------------------------------------------------------------------
    # ruff: N806 — class-named local is intentional; this is a class object
    # and the rest of the module / consumers refer to it as ``CreateJobRequest``.
    CreateJobRequest = _build_request_model(payload_schemas)  # noqa: N806

    router = APIRouter(
        prefix="/api/jobs",
        tags=["jobs"],
        responses={
            400: {"model": ErrorResponse},
            404: {"model": ErrorResponse},
            500: {"model": ErrorResponse},
        },
    )

    # ------------------------------------------------------------------
    # POST /api/jobs
    # ------------------------------------------------------------------
    @router.post("", status_code=202, response_model=JobCreateResponse)
    @limiter.limit("30/minute")
    async def create_job(
        request: Request,
        body: CreateJobRequest,  # type: ignore[valid-type]
        db_pool: asyncpg.Pool = Depends(get_db_pool),
        user_id: int = Depends(current_user_id_strict),
    ) -> JobCreateResponse:
        """Enqueue a new background job and return its ID."""
        import uuid as _uuid

        from jarvis_common.task_registry import KIND_TO_TASK

        kinds_now = _public_kinds_now()
        if body.kind not in kinds_now:
            # Discriminated mode already filtered shape errors with 422 at
            # parse time; reaching this branch means either:
            #   - permissive mode (no schemas) with an unknown kind → 400
            #   - discriminated mode with a kind that is in the union but
            #     not in the runtime allowlist (e.g. noop.test with the
            #     env toggle off) → 422 to mirror parse-time semantics.
            status_code = 422 if payload_schemas else 400
            raise HTTPException(
                status_code=status_code,
                detail=f"Job kind {body.kind!r} is not allowed. "
                f"Permitted kinds: {sorted(kinds_now)}",
            )
        # RD-DA-001/002: paper-scoped ownership check before enqueue.
        # user_id is always an int here (current_user_id_strict enforces it).
        if paper_ownership_extractor is not None:
            paper_id_for_check = paper_ownership_extractor(body.payload)
            if isinstance(paper_id_for_check, int):
                async with db_pool.acquire() as conn:
                    # PoolConnectionProxy delegates fetchrow → real Connection at
                    # runtime; the helper signature is asyncpg.Connection.
                    await assert_paper_ownership(conn, paper_id_for_check, user_id)  # type: ignore[arg-type]
            elif isinstance(paper_id_for_check, list):
                async with db_pool.acquire() as conn:
                    await assert_papers_ownership(conn, paper_id_for_check, user_id)  # type: ignore[arg-type]

        # Dispatch via procrastinate task registry.
        tasks = task_lookup() if task_lookup is not None else KIND_TO_TASK
        task = tasks.get(body.kind)
        if task is None:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown kind {body.kind!r}",
            )
        payload = dict(body.payload or {})
        if "job_id" in payload or "user_id" in payload:
            raise HTTPException(
                status_code=400,
                detail="payload may not contain reserved keys 'job_id' or 'user_id'",
            )
        jarvis_job_id = str(_uuid.uuid4())
        await task.defer_async(
            job_id=jarvis_job_id,
            user_id=user_id,
            **payload,
        )
        return JobCreateResponse(job_id=jarvis_job_id, status="queued")

    # ------------------------------------------------------------------
    # GET /api/jobs/{job_id}
    # ------------------------------------------------------------------
    @router.get("/{job_id}", response_model=JobStatusResponse)
    @limiter.limit("120/minute")
    async def get_job(
        request: Request,
        job_id: str,
        db_pool: asyncpg.Pool = Depends(get_db_pool),
        user_id: int = Depends(current_user_id_strict),
    ) -> dict[str, Any]:
        """Return the full job row for the given job_id."""
        row = await jobs_lib.get_unified(db_pool, job_id)
        if row is None or not _owner_matches(row.get("user_id"), user_id):
            raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")
        return serialise_row(row)

    # ------------------------------------------------------------------
    # GET /api/jobs
    # ------------------------------------------------------------------
    @router.get("", response_model=list[JobStatusResponse])
    @limiter.limit("60/minute")
    async def list_jobs(
        request: Request,
        status: str | None = Query(default=None),
        kind: str | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=500),
        db_pool: asyncpg.Pool = Depends(get_db_pool),
        user_id: int = Depends(current_user_id_strict),
    ) -> list[dict[str, Any]]:
        """Return a list of jobs, optionally filtered by status and/or kind."""
        rows = await jobs_lib.list_jobs(
            db_pool,
            status=status,
            kind=kind,
            limit=limit,
            user_id=str(user_id),
        )
        return [serialise_row(r) for r in rows]

    # ------------------------------------------------------------------
    # GET /api/jobs/{job_id}/stream  — SSE
    # ------------------------------------------------------------------
    @router.get("/{job_id}/stream")
    @limiter.limit("10/minute")
    async def stream_job(
        request: Request,
        job_id: str,
        db_pool: asyncpg.Pool = Depends(get_db_pool),
        user_id: int = Depends(current_user_id_strict),
    ) -> StreamingResponse:
        """SSE stream of progress updates for the given job.

        Closes automatically on terminal status (succeeded/failed/cancelled).
        Returns 404 (not 403) on ownership mismatch to avoid leaking job
        existence to unauthorized callers.

        Bug 2 fix: previously called ``jobs_lib.get`` which only queries the
        legacy ``jobs`` table; procrastinate-only jobs therefore returned 404.
        ``get_unified`` falls through to the procrastinate table when the
        legacy lookup misses.
        """
        initial = await jobs_lib.get_unified(db_pool, job_id)
        if initial is None or not _owner_matches(initial.get("user_id"), user_id):
            raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")

        return StreamingResponse(
            jobs_lib.stream_job_events(db_pool, job_id, is_disconnected=request.is_disconnected),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    # ------------------------------------------------------------------
    # POST /api/jobs/{job_id}/cancel
    # ------------------------------------------------------------------
    @router.post("/{job_id}/cancel")
    @limiter.limit("30/minute")
    async def cancel_job(
        request: Request,
        job_id: str,
        db_pool: asyncpg.Pool = Depends(get_db_pool),
        user_id: int = Depends(current_user_id_strict),
    ) -> dict[str, Any]:
        """Request cancellation of a running or queued job."""
        row = await jobs_lib.get_unified(db_pool, job_id)
        if row is None or not _owner_matches(row.get("user_id"), user_id):
            raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")

        from jarvis_common.task_registry import app as procrastinate_app

        prow = await jobs_lib.get_procrastinate_job_for_jarvis_id(db_pool, job_id)
        if prow:
            await procrastinate_app.job_manager.cancel_job_by_id_async(prow["id"], abort=True)
        return {"ok": True}

    # Expose the request model on the router so service-level shims (and
    # tests that need to construct a request directly) can import it.
    router.create_job_request_model = CreateJobRequest  # type: ignore[attr-defined]
    return router


# ---------------------------------------------------------------------------
# Request-model construction
# ---------------------------------------------------------------------------


def _build_request_model(
    payload_schemas: dict[str, type[BaseModel]] | None,
) -> type[BaseModel]:
    """Return a ``CreateJobRequest`` model appropriate for the given mode.

    * ``payload_schemas`` set → discriminated-union validation (422 on
      unknown kind / wrong shape).
    * ``payload_schemas`` falsy → permissive validation: ``kind`` non-empty,
      ``payload`` is an arbitrary dict.

    Both variants use ``Field(default_factory=dict)`` to avoid SYM-002
    (mutable default sharing).
    """
    if not payload_schemas:
        return _build_permissive_request_model()
    return _build_discriminated_request_model(payload_schemas)


def _build_permissive_request_model() -> type[BaseModel]:
    """LE-style: ``kind: str`` (non-empty) + ``payload: dict``."""

    class CreateJobRequest(BaseModel):
        kind: str
        payload: dict[str, Any] = Field(default_factory=dict)

        @field_validator("kind")
        @classmethod
        def _kind_nonempty(cls, v: str) -> str:
            if not v or not v.strip():
                raise ValueError("kind must be a non-empty string")
            return v

    return CreateJobRequest


def _build_discriminated_request_model(
    payload_schemas: dict[str, type[BaseModel]],
) -> type[BaseModel]:
    """PI-style: each kind has a payload schema validated via discriminated union."""
    schemas = list(payload_schemas.values())
    if len(schemas) == 1:
        # A single-arm union is illegal for Annotated[..., discriminator=...];
        # fall back to the bare schema.  (The runtime allowlist still applies.)
        union_type: Any = schemas[0]
    else:
        # Build ``Schema1 | Schema2 | ...`` dynamically.
        union_type = schemas[0]
        for extra in schemas[1:]:
            union_type = union_type | extra
    payload_union = Annotated[union_type, Field(discriminator="kind")]
    adapter: TypeAdapter[Any] = TypeAdapter(payload_union)

    class CreateJobRequest(BaseModel):
        """Validated job-creation request (discriminated mode).

        Wire format ``{"kind": "...", "payload": {...}}`` is preserved.
        The model_validator merges ``kind`` into the payload dict and parses
        the result through the discriminated union, so unknown kinds and
        missing / wrong-typed required fields are rejected with HTTP 422
        before the handler runs.
        """

        kind: str
        payload: dict[str, Any] = Field(default_factory=dict)

        @model_validator(mode="after")
        def _validate_payload_for_kind(self) -> "CreateJobRequest":
            merged: dict[str, Any] = {**self.payload, "kind": self.kind}
            adapter.validate_python(merged)
            return self

    return CreateJobRequest
