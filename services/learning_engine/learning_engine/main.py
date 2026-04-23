"""Learning Engine Service - FastAPI application.

Thin entrypoint: lifespan, middleware, error handlers, health check,
and router registration.  Endpoint logic lives in ``learning_engine.routers.*`` modules.
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager

import asyncpg
import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from jarvis_common import (
    HealthCheckResponse,
    RequestIDMiddleware,
    configure_logging,
    generic_exception_handler,
    http_exception_handler,
    init_pg_connection,
    rate_limit_exceeded_handler,
    read_secret,
    validate_production_config,
    validation_exception_handler,
    verify_api_key,
)
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from learning_engine.anki_exporter import AnkiExporter
from learning_engine.card_generator import CardGenerator
from learning_engine.deps import limiter
from learning_engine.fsrs_manager import FSRSManager

configure_logging("learning_engine", log_level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize and tear down shared resources."""
    validate_production_config()

    from jarvis_common.llm_client import LITELLM_FALLBACK_ENV_NAMES, get_litellm_config

    database_url = os.environ["DATABASE_URL"]

    app.state.db_pool = await asyncpg.create_pool(
        database_url,
        min_size=int(os.environ.get("DB_POOL_MIN", "2")),
        max_size=int(os.environ.get("DB_POOL_MAX", "10")),
        init=init_pg_connection,
    )
    app.state.http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0),
    )

    # Load desired_retention from user_config (default 0.9)
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

    dev_mode = os.environ.get("DEV_MODE", "false").lower() == "true"
    api_key = read_secret("JARVIS_API_KEY")
    if api_key:
        logger.info("API key authentication enabled")
    elif dev_mode:
        logger.info("DEV_MODE enabled -- running without authentication")
    else:
        logger.warning(
            "API authentication is not configured and DEV_MODE is disabled -- service will reject requests"  # noqa: E501
        )

    # Start the jobs worker — handles all job kinds owned by this service.
    # Explicitly import routers that register @job_handler decorators so the
    # handlers are present in the registry before the worker begins polling.
    import importlib

    from jarvis_common import jobs as jobs_lib

    importlib.import_module("learning_engine.routers.generation")

    _kinds_learning_engine: set[str] = {
        "card.generate",
        "card.generate_batch",
    }
    _jobs_stop = asyncio.Event()
    app.state.jobs_worker_stop = _jobs_stop
    app.state.jobs_worker_task = asyncio.create_task(
        jobs_lib.worker_loop(
            app.state.db_pool,
            app.state.http_client,
            kinds=_kinds_learning_engine,
            stop_event=_jobs_stop,
        )
    )

    logger.info("Learning Engine Service started (retention=%.2f)", desired_retention)
    yield

    # Stop the jobs worker gracefully.
    app.state.jobs_worker_stop.set()
    try:
        await asyncio.wait_for(app.state.jobs_worker_task, timeout=5.0)
    except (TimeoutError, asyncio.CancelledError):
        logger.warning("jobs worker did not stop in time — cancelling task")
        app.state.jobs_worker_task.cancel()

    await app.state.http_client.aclose()
    await app.state.db_pool.close()
    logger.info("Learning Engine Service stopped")


# ---------------------------------------------------------------------------
# App creation + middleware + error handlers
# ---------------------------------------------------------------------------

app = FastAPI(
    title="JARVIS Learning Engine",
    description="FSRS-based spaced repetition card management",
    version="0.1.0",
    lifespan=lifespan,
    dependencies=[Depends(verify_api_key)],
)

# Middleware registration order (Starlette: last-added = outermost = runs first):
#   1. CORS  — must be outermost so OPTIONS preflight is handled before anything else
#   2. SlowAPIMiddleware — global 600/minute cap runs pre-auth (auth is a route Depends)
#   3. RequestIDMiddleware — assigns request ID before further processing

# RequestIDMiddleware (innermost middleware — added first)
app.add_middleware(RequestIDMiddleware)

# Rate limiting — pre-auth global cap; runs before route-level auth Depends.
# SlowAPIMiddleware reads app.state.limiter (set below) for per-route limits
# and also enforces a 600/minute global cap via the default_limits on the limiter.
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

# CORS — added before ProxyHeadersMiddleware so CORS runs after proxy unwrapping
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        o.strip()
        for o in os.environ.get("CORS_ORIGINS", "https://localhost:3001").split(",")
        if o.strip()
    ],
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-API-Key"],
)

# ProxyHeadersMiddleware (outermost — added last so it runs first, decoding
# X-Forwarded-For / X-Forwarded-Proto before any other middleware sees the request)
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")

# Standardized error handlers
app.add_exception_handler(StarletteHTTPException, http_exception_handler)
app.add_exception_handler(RequestValidationError, validation_exception_handler)
app.add_exception_handler(Exception, generic_exception_handler)

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
