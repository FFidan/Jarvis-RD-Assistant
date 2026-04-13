"""Learning Engine Service - FastAPI application.

Thin entrypoint: lifespan, middleware, error handlers, health check,
and router registration.  Endpoint logic lives in ``app.routers.*`` modules.
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
    RequestIDMiddleware,
    configure_logging,
    generic_exception_handler,
    http_exception_handler,
    init_pg_connection,
    rate_limit_exceeded_handler,
    validate_production_config,
    validation_exception_handler,
    verify_api_key,
)
from slowapi.errors import RateLimitExceeded
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.anki_exporter import AnkiExporter
from app.card_generator import CardGenerator
from app.deps import limiter
from app.fsrs_manager import FSRSManager
from app.models import HealthCheckResponse

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
        logger.warning("Could not load fsrs.desired_retention, using default 0.9", exc_info=True)

    litellm_config = get_litellm_config(fallback_env_names=LITELLM_FALLBACK_ENV_NAMES)
    app.state.fsrs_manager = FSRSManager(desired_retention=desired_retention)
    app.state.card_generator = CardGenerator(
        http_client=app.state.http_client,
        litellm_config=litellm_config,
    )
    app.state.anki_exporter = AnkiExporter()

    dev_mode = os.environ.get("DEV_MODE", "false").lower() == "true"
    api_key = os.environ.get("JARVIS_API_KEY", "")
    if api_key:
        logger.info("API key authentication enabled")
    elif dev_mode:
        logger.info("DEV_MODE enabled -- running without authentication")
    else:
        logger.warning(
            "API authentication is not configured and DEV_MODE is disabled -- service will reject requests"  # noqa: E501
        )

    logger.info("Learning Engine Service started (retention=%.2f)", desired_retention)
    yield

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

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("CORS_ORIGINS", "http://localhost:3000").split(","),
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-API-Key"],
)
app.add_middleware(RequestIDMiddleware)

# Standardized error handlers
app.add_exception_handler(StarletteHTTPException, http_exception_handler)
app.add_exception_handler(RequestValidationError, validation_exception_handler)
app.add_exception_handler(Exception, generic_exception_handler)

# Rate limiting
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)

# ---------------------------------------------------------------------------
# Router registration
# ---------------------------------------------------------------------------

from app.routers import (  # noqa: E402
    analytics,
    cards,
    decks,
    executive,
    export,
    generation,
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


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


@app.get("/health", dependencies=[], response_model=HealthCheckResponse)
async def health_check(request: Request) -> HealthCheckResponse | JSONResponse:
    """Return service health status with dependency probing (no auth required)."""
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
    body = {"status": status, "service": "learning_engine", "checks": checks}
    if status == "degraded":
        return JSONResponse(status_code=503, content=body)
    return body
