"""Learning Engine Service - FastAPI application.

Thin entrypoint: lifespan, middleware, error handlers, health check,
and router registration.  Endpoint logic lives in ``learning_engine.routers.*`` modules.

Lifespan + middleware + error handlers are wired via
:func:`jarvis_common.configure_lifespan` /
:func:`jarvis_common.configure_middleware_and_errors` (DRY-002 -- the shared
factory).  Service-specific init (FSRS retention loading, CardGenerator,
AnkiExporter) lives in ``custom_init_tasks`` hooks below.
"""

import asyncio
import logging
import os

import asyncpg
import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, ORJSONResponse
from jarvis_common import (
    HealthCheckResponse,
    ServiceLifespanConfig,
    configure_lifespan,
    configure_logging,
    configure_middleware_and_errors,
    verify_api_key,
)
from jarvis_common.settings import get_core_settings

from learning_engine.anki_exporter import AnkiExporter
from learning_engine.card_generator import CardGenerator
from learning_engine.deps import limiter
from learning_engine.fsrs_manager import FSRSManager

configure_logging("learning_engine", log_level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

try:
    import orjson as _orjson  # noqa: F401

    DEFAULT_RESPONSE_CLASS = ORJSONResponse
except ImportError:
    logger.warning("orjson is not installed; falling back to JSONResponse")
    DEFAULT_RESPONSE_CLASS = JSONResponse


# ---------------------------------------------------------------------------
# Lifespan custom hooks (DRY-002 — orchestrated by jarvis_common.app_factory)
# ---------------------------------------------------------------------------


async def _warn_multitenant_stub(app: FastAPI) -> None:
    """C1 doc: log CRITICAL when MULTITENANT_ENABLED=true because auth resolver is a stub."""
    if os.getenv("MULTITENANT_ENABLED", "false").lower() == "true":
        logger.critical(
            "MULTITENANT_ENABLED=true but auth resolver is a stub — ownership checks are no-ops"
        )


async def _init_fsrs_and_generators(app: FastAPI) -> None:
    """Load FSRS retention from user_config + construct CardGenerator + AnkiExporter."""
    from jarvis_common.llm_client import LITELLM_FALLBACK_ENV_NAMES, get_litellm_config

    desired_retention = 0.9
    try:
        async with app.state.db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT value FROM user_config WHERE key = $1",
                "fsrs.desired_retention",
            )
            if row:
                desired_retention = float(row["value"])
    except Exception:
        logger.error("Could not load fsrs.desired_retention, using default 0.9", exc_info=True)

    litellm_config = get_litellm_config(fallback_env_names=LITELLM_FALLBACK_ENV_NAMES)
    app.state.fsrs_manager = FSRSManager(desired_retention=desired_retention)
    app.state.card_generator = CardGenerator(
        http_client=app.state.http_client,
        litellm_config=litellm_config,
    )
    app.state.anki_exporter = AnkiExporter()
    # Stash the value for the post-init log line.
    app.state._fsrs_desired_retention = desired_retention


async def _register_le_job_handlers(app: FastAPI) -> None:
    """Import modules whose ``@job_handler`` decorators must run before the worker."""
    import importlib  # noqa: PLC0415

    importlib.import_module("learning_engine.routers.generation")


async def _log_le_started(app: FastAPI) -> None:
    """Echo the original ``Service started (retention=0.90)`` log line."""
    retention = getattr(app.state, "_fsrs_desired_retention", 0.9)
    logger.info("Learning Engine init complete (retention=%.2f)", retention)


_LEARNING_ENGINE_JOB_KINDS: set[str] = {
    "card.generate",
    "card.generate_batch",
}


# ---------------------------------------------------------------------------
# App creation + middleware + error handlers
# ---------------------------------------------------------------------------

_lifespan_config = ServiceLifespanConfig(
    service_name="Learning Engine Service",
    jobs_worker_kinds=_LEARNING_ENGINE_JOB_KINDS,
    custom_init_tasks=[
        _warn_multitenant_stub,
        _init_fsrs_and_generators,
        _register_le_job_handlers,
        _log_le_started,
    ],
)

app = FastAPI(
    title="JARVIS Learning Engine",
    description="FSRS-based spaced repetition card management",
    version="0.1.0",
    lifespan=configure_lifespan(_lifespan_config),
    dependencies=[Depends(verify_api_key)],
    default_response_class=DEFAULT_RESPONSE_CLASS,
)

configure_middleware_and_errors(
    app, limiter=limiter, trusted_proxy_hosts=get_core_settings().trusted_proxy_hosts_list
)

# ---------------------------------------------------------------------------
# Router registration
# ---------------------------------------------------------------------------

from learning_engine.routers import (  # noqa: E402
    analytics,
    cards,
    decks,
    executive,
    export,
    generation,
    jobs,
    milestones,
    project_papers,
    projects,
    review,
    tasks,
)

app.include_router(projects.router)
app.include_router(tasks.router)
app.include_router(milestones.router)
app.include_router(analytics.router)
app.include_router(project_papers.router)
app.include_router(decks.router)
app.include_router(cards.router)
app.include_router(review.router)
app.include_router(generation.router)
app.include_router(export.router)
app.include_router(executive.router)
app.include_router(jobs.router)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


async def _run_health_checks(request: Request) -> tuple[str, dict[str, str]]:
    """Execute all dependency probes and return (status, checks)."""
    checks: dict[str, str] = {}

    # Check PostgreSQL
    try:
        pool: asyncpg.Pool = request.app.state.db_pool
        async with pool.acquire() as conn:
            await asyncio.wait_for(conn.fetchval("SELECT 1"), timeout=5.0)
        checks["postgres"] = "ok"
    except Exception:
        checks["postgres"] = "unavailable"

    # Check LiteLLM
    try:
        from jarvis_common.llm_client import get_litellm_config

        litellm_config = get_litellm_config()
        client: httpx.AsyncClient = request.app.state.http_client
        resp = await client.get(f"{litellm_config.base_url}/health/readiness", timeout=5.0)
        checks["litellm"] = "ok" if resp.status_code == 200 else "unavailable"
    except Exception:
        checks["litellm"] = "unavailable"

    all_ok = all(v == "ok" for v in checks.values())
    status = "ok" if all_ok else "degraded"
    return status, checks


@app.get("/health", dependencies=[], response_model=None)
async def health_check(request: Request) -> dict[str, str] | JSONResponse:
    """Public health probe — returns only ``{"status": "ok"|"degraded"|"down"}``.

    No dependency details are exposed to unauthenticated callers.
    Docker healthchecks and upstreams check the HTTP status code: 200 = ok,
    503 = degraded.  Use ``GET /health/internal`` (requires auth) for the full
    dependency breakdown.
    """
    status, _ = await _run_health_checks(request)
    content: dict[str, str] = {"status": status}
    if status == "degraded":
        return JSONResponse(status_code=503, content=content)
    return content


@app.get("/health/internal", response_model=HealthCheckResponse)
async def health_check_internal(
    request: Request,
    _auth: None = Depends(verify_api_key),
) -> HealthCheckResponse | JSONResponse:
    """Authenticated health probe — returns full dependency details.

    Includes individual check results for PostgreSQL and LiteLLM.
    Requires a valid API key.  Returns HTTP 503 when any dependency is
    unavailable.
    """
    status, checks = await _run_health_checks(request)
    body = {"status": status, "service": "learning_engine", "checks": checks}
    if status == "degraded":
        return JSONResponse(status_code=503, content=body)
    return body
