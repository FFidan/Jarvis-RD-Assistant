"""Paper Ingestion Service - FastAPI application.

Thin entrypoint: lifespan, middleware, error handlers, health check,
and router registration.  Endpoint logic lives in
``paper_ingestion.routers.*`` modules.

Extracted modules
-----------------
* ``paper_ingestion.migrations_runner`` — ``run_migrations()``
* ``paper_ingestion.services.telegram_bootstrap`` — ``refresh_telegram_bot_username()``
* ``paper_ingestion.routers.system`` — ``GET /api/system/models``
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

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
from jarvis_common.llm_client import get_litellm_config
from qdrant_client import AsyncQdrantClient
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

# Trigger source registration via imports
import paper_ingestion.sources  # noqa: F401
from paper_ingestion.deps import limiter
from paper_ingestion.ingestion.embedder import Embedder
from paper_ingestion.migrations_runner import run_migrations
from paper_ingestion.models import PaperSourceConfig, SourceType
from paper_ingestion.pdf_processor import PDFProcessor
from paper_ingestion.services.telegram_bootstrap import refresh_telegram_bot_username
from paper_ingestion.sources.registry import get_source_class
from paper_ingestion.verification import QuoteVerifier

configure_logging("paper_ingestion", log_level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize and tear down shared resources.

    Creates asyncpg connection pool, httpx client, Qdrant client,
    and all service objects.  Stored on ``app.state`` and accessed
    via ``Depends()`` in endpoints.
    """
    validate_production_config()

    database_url = os.environ["DATABASE_URL"]
    qdrant_url = os.environ.get("QDRANT_URL", "http://qdrant:6333")

    app.state.db_pool = await asyncpg.create_pool(
        database_url,
        min_size=int(os.environ.get("DB_POOL_MIN", "2")),
        max_size=int(os.environ.get("DB_POOL_MAX", "10")),
        init=init_pg_connection,
    )
    await run_migrations(app.state.db_pool)
    app.state.http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0),
    )
    qdrant_api_key = os.environ.get("QDRANT_API_KEY") or None
    app.state.qdrant_client = AsyncQdrantClient(
        url=qdrant_url, api_key=qdrant_api_key, check_compatibility=False
    )
    app.state.embedder = Embedder(app.state.http_client, app.state.qdrant_client)
    await app.state.embedder.ensure_collection()
    app.state.pdf_processor = PDFProcessor(app.state.http_client, app.state.embedder)
    app.state.verifier = QuoteVerifier()

    # Populate module-level service state so job handlers can access these
    # objects without importing paper_ingestion.main (which would be circular).
    from paper_ingestion._state import svc  # noqa: PLC0415

    svc.pdf_processor = app.state.pdf_processor
    svc.embedder = app.state.embedder
    svc.verifier = app.state.verifier

    # C-8: Initialize source singletons so the rate limiter persists across requests.
    app.state.sources = {}
    _preloaded_sources = [
        SourceType.ARXIV,
        SourceType.SEMANTIC_SCHOLAR,
        SourceType.PUBMED,
        SourceType.OPENALEX,
    ]
    for _source_type in _preloaded_sources:
        _source_type_val = _source_type.value
        try:
            _source_cls = get_source_class(_source_type_val)
            if not _source_cls:
                continue
            async with app.state.db_pool.acquire() as _conn:
                _row = await _conn.fetchrow(
                    "SELECT id, source_type, enabled, config"
                    " FROM paper_sources WHERE source_type = $1",
                    _source_type_val,
                )
            if _row:
                _config = PaperSourceConfig(
                    id=_row["id"],
                    source_type=_row["source_type"],
                    enabled=_row["enabled"],
                    config=_row["config"] or {},
                )
                app.state.sources[_source_type_val] = _source_cls(_config, app.state.http_client)
        except Exception:
            logger.warning(
                "Could not initialize source singleton for %s",
                _source_type_val,
                exc_info=True,
            )

    # Expose sources through the module-level state so pulse job can reach it.
    svc.sources = app.state.sources

    # Refresh the cached Telegram bot username (used by the setup wizard
    # to build pairing deep-links). Never raises on failure.
    await refresh_telegram_bot_username(app.state.db_pool, app.state.http_client)

    dev_mode = os.environ.get("DEV_MODE", "false").lower() == "true"
    api_key = read_secret("JARVIS_API_KEY")
    if api_key:
        logger.info("API key authentication enabled")
    elif dev_mode:
        logger.info("DEV_MODE enabled -- running without authentication")
    else:
        logger.warning(
            "JARVIS_API_KEY not set and DEV_MODE not enabled -- service will reject requests"
        )

    # Always start the scheduler so live-toggles (interval / pulse.enabled) take effect
    # without requiring a restart.  Each job self-gates on its own condition at runtime.
    _interval = float(os.environ.get("AUTO_FETCH_INTERVAL_HOURS", "0"))
    from .scheduler import start_scheduler

    app.state.scheduler = await start_scheduler(app, interval_hours=_interval)

    # Register job handlers before the worker starts (decorator side-effects).
    # importlib avoids the shadowing conflict between parameter `app: FastAPI` and
    # the `app` package namespace that a bare `import paper_ingestion.*` would create.
    import importlib  # noqa: PLC0415

    from jarvis_common import jobs as jobs_lib  # noqa: PLC0415

    importlib.import_module("paper_ingestion.paper_jobs")
    importlib.import_module("paper_ingestion.extraction_jobs")
    importlib.import_module("paper_ingestion.pulse.job")
    importlib.import_module("paper_ingestion.integrations.zotero_service")

    _kinds_paper_ingestion: set[str] = {
        "pulse.generate",
        "paper.download",
        "paper.process",
        "paper.analyze",
        "paper.summarize",
        "papers.batch_summarize",
        "papers.batch_process",
        "papers.scan_local",
        "extraction.single",
        "extraction.batch",
        "citations.batch_fetch",
        "digest.weekly",
        "zotero.push",
        "zotero.resync",
        "zotero.sync_from_zotero",
    }
    _jobs_stop = asyncio.Event()
    app.state.jobs_worker_stop = _jobs_stop
    app.state.jobs_worker_task = asyncio.create_task(
        jobs_lib.worker_loop(
            app.state.db_pool,
            app.state.http_client,
            kinds=_kinds_paper_ingestion,
            stop_event=_jobs_stop,
        )
    )

    logger.info("Paper Ingestion Service started")
    yield

    # Stop the jobs worker gracefully.
    app.state.jobs_worker_stop.set()
    try:
        await asyncio.wait_for(app.state.jobs_worker_task, timeout=5.0)
    except (TimeoutError, asyncio.CancelledError):
        logger.warning("jobs worker did not stop in time — cancelling task")
        app.state.jobs_worker_task.cancel()

    if hasattr(app.state, "scheduler"):
        app.state.scheduler.shutdown(wait=False)

    await app.state.qdrant_client.close()
    await app.state.http_client.aclose()
    await app.state.db_pool.close()
    logger.info("Paper Ingestion Service stopped")


# ---------------------------------------------------------------------------
# App creation + middleware + error handlers
# ---------------------------------------------------------------------------

app = FastAPI(
    title="JARVIS Paper Ingestion",
    description="Paper fetching, PDF processing, and embedding service",
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

from paper_ingestion.routers import (  # noqa: E402
    analyze,
    authors,
    citations,
    dashboard_api,
    extractions,
    jobs,
    knowledge_graph,
    notes,
    papers,
    pdf,
    priority,
    rag,
    recommendations,
    search,
    settings,
    snapshots,
    system,
    telegram,
    topics,
)
from paper_ingestion.routers import pulse as pulse_router  # noqa: E402
from paper_ingestion.routers import zotero as zotero_router  # noqa: E402

app.include_router(topics.router)
app.include_router(settings.router)
app.include_router(snapshots.router)
app.include_router(authors.router)
app.include_router(citations.router)
app.include_router(extractions.router)
app.include_router(knowledge_graph.router)
app.include_router(dashboard_api.router)
app.include_router(analyze.router)
app.include_router(notes.router)
app.include_router(priority.router)
app.include_router(recommendations.router)
app.include_router(search.router)
app.include_router(papers.router)
app.include_router(pdf.router)
app.include_router(rag.router)
app.include_router(pulse_router.router)
app.include_router(zotero_router.router)
app.include_router(telegram.router)
app.include_router(system.router)
app.include_router(jobs.router)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


async def _run_health_checks(request: Request) -> tuple[str, dict[str, str]]:
    """Execute all dependency probes and return (status, checks)."""
    checks: dict[str, str] = {}

    # PostgreSQL
    try:
        async with request.app.state.db_pool.acquire() as conn:
            await asyncio.wait_for(conn.execute("SELECT 1"), timeout=5.0)
        checks["postgres"] = "ok"
    except Exception:
        logger.warning("Health check: PostgreSQL unavailable", exc_info=True)
        checks["postgres"] = "unavailable"

    # Qdrant
    try:
        await asyncio.wait_for(request.app.state.qdrant_client.get_collections(), timeout=5.0)
        checks["qdrant"] = "ok"
    except Exception:
        logger.warning("Health check: Qdrant unavailable", exc_info=True)
        checks["qdrant"] = "unavailable"

    # LiteLLM
    try:
        litellm_config = get_litellm_config()
        resp = await asyncio.wait_for(
            request.app.state.http_client.get(f"{litellm_config.base_url}/health/readiness"),
            timeout=5.0,
        )
        checks["litellm"] = "ok" if resp.status_code == 200 else "unavailable"
    except Exception:
        logger.warning("Health check: LiteLLM unavailable", exc_info=True)
        checks["litellm"] = "unavailable"

    status = "ok" if all(v == "ok" for v in checks.values()) else "degraded"
    return status, checks


@app.get("/health", dependencies=[], response_model=None)
async def health_check(request: Request) -> dict[str, Any]:
    """Public health probe — returns only ``{"status": "ok"|"degraded"|"down"}``.

    No dependency details are exposed to unauthenticated callers.
    Docker healthchecks and upstreams check the HTTP status code: 200 = ok,
    503 = degraded.  Use ``GET /health/internal`` (requires auth) for the full
    dependency breakdown.
    """
    status, _ = await _run_health_checks(request)
    content = {"status": status}
    if status == "degraded":
        return JSONResponse(status_code=503, content=content)  # type: ignore[return-value]
    return content


@app.get("/health/internal", response_model=HealthCheckResponse)
async def health_check_internal(
    request: Request,
    _auth: None = Depends(verify_api_key),
) -> HealthCheckResponse:
    """Authenticated health probe — returns full dependency details.

    Includes individual check results for PostgreSQL, Qdrant, and LiteLLM.
    Requires a valid API key.  Returns HTTP 503 when any dependency is
    unavailable.
    """
    status, checks = await _run_health_checks(request)
    body = HealthCheckResponse(status=status, service="paper_ingestion", checks=checks)
    if status == "degraded":
        return JSONResponse(status_code=503, content=body.model_dump())  # type: ignore[return-value]
    return body


# ---------------------------------------------------------------------------
# Back-compat shims (imported by tests and internal lazy imports)
# ---------------------------------------------------------------------------

# run_migrations is imported directly from paper_ingestion.migrations_runner;
# re-export here so existing `from paper_ingestion.main import run_migrations` still works.
# (already imported at top of file)

# get_system_models is now served by paper_ingestion.routers.system;
# re-export the router function for test_brief_and_models.py back-compat.
from paper_ingestion.routers.system import get_system_models  # noqa: E402,F401
