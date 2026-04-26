"""Paper Ingestion Service - FastAPI application.

Thin entrypoint: lifespan, middleware, error handlers, health check,
and router registration.  Endpoint logic lives in
``paper_ingestion.routers.*`` modules.

Extracted modules
-----------------
* ``paper_ingestion.migrations_runner`` — ``run_migrations()``
* ``paper_ingestion.services.telegram_bootstrap`` — ``refresh_telegram_bot_username()``
* ``paper_ingestion.routers.system`` — ``GET /api/system/models``

Lifespan + middleware + error handlers are wired via
:func:`jarvis_common.configure_lifespan` /
:func:`jarvis_common.configure_middleware_and_errors` (DRY-002 -- the shared
factory keeps both microservices in lockstep on the cross-cutting concerns
while letting paper_ingestion express its rich init pipeline as
``custom_init_tasks`` hooks).
"""

import asyncio
import logging
import os
from typing import Any

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
from jarvis_common.llm_client import get_litellm_config
from jarvis_common.settings import get_core_settings
from qdrant_client import AsyncQdrantClient

# Trigger source registration via imports
import paper_ingestion.sources  # noqa: F401
from paper_ingestion.deps import limiter
from paper_ingestion.ingestion.embedder import Embedder
from paper_ingestion.integrations.zotero_client import validate_bbt_base_url
from paper_ingestion.migrations_runner import run_migrations
from paper_ingestion.models import PaperSourceConfig, SourceType
from paper_ingestion.pdf_processor import PDFProcessor
from paper_ingestion.services.telegram_bootstrap import refresh_telegram_bot_username
from paper_ingestion.sources.registry import get_source_class
from paper_ingestion.verification import QuoteVerifier

# WS-6: install uvloop early so the event-loop policy is set before
# any asyncio.get_event_loop() calls.  Guarded against pytest runs because
# uvloop.install() mutates the global policy and breaks pytest-asyncio
# per-test loop isolation (tests pass when isolated but fail as a suite).
if not os.environ.get("PYTEST_CURRENT_TEST"):
    try:
        import uvloop  # noqa: PLC0415

        uvloop.install()
    except ImportError:
        pass

configure_logging("paper_ingestion", log_level=os.environ.get("LOG_LEVEL", "INFO"))
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


async def _validate_bbt_url_hook(app: FastAPI) -> None:
    """PI-EDGE-008: validate ``BBT_BASE_URL`` to block file:// + unknown private IPs.

    Must run before any Zotero integration touches the URL — the SSRF surface
    area is the BBT translator endpoint.
    """
    validate_bbt_base_url()


async def _run_migrations_hook(app: FastAPI) -> None:
    """Apply DB migrations idempotently before any other init touches the schema."""
    await run_migrations(app.state.db_pool)


async def _init_qdrant_and_pdf_pipeline(app: FastAPI) -> None:
    """Construct Qdrant client + Embedder + PDFProcessor + QuoteVerifier."""
    qdrant_url = os.environ.get("QDRANT_URL", "http://qdrant:6333")
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


async def _init_source_singletons(app: FastAPI) -> None:
    """C-8: Initialize source singletons so rate-limiter state persists across requests."""
    app.state.sources = {}
    preloaded_sources = [
        SourceType.ARXIV,
        SourceType.SEMANTIC_SCHOLAR,
        SourceType.PUBMED,
        SourceType.OPENALEX,
    ]
    for source_type in preloaded_sources:
        source_type_val = source_type.value
        try:
            source_cls = get_source_class(source_type_val)
            if not source_cls:
                continue
            async with app.state.db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT id, source_type, enabled, config"
                    " FROM paper_sources WHERE source_type = $1",
                    source_type_val,
                )
            if row:
                config = PaperSourceConfig(
                    id=row["id"],
                    source_type=row["source_type"],
                    enabled=row["enabled"],
                    config=row["config"] or {},
                )
                app.state.sources[source_type_val] = source_cls(config, app.state.http_client)
        except Exception:
            logger.warning(
                "Could not initialize source singleton for %s",
                source_type_val,
                exc_info=True,
            )

    # Expose sources through module-level state so the pulse job can reach it.
    from paper_ingestion._state import svc  # noqa: PLC0415

    svc.sources = app.state.sources


async def _refresh_telegram_username(app: FastAPI) -> None:
    """Refresh cached Telegram bot username for setup-wizard pairing links."""
    await refresh_telegram_bot_username(app.state.db_pool, app.state.http_client)


async def _start_scheduler_hook(app: FastAPI) -> None:
    """Always start the scheduler so live toggles take effect without restart."""
    interval = float(os.environ.get("AUTO_FETCH_INTERVAL_HOURS", "0"))
    from .scheduler import start_scheduler  # noqa: PLC0415

    app.state.scheduler = await start_scheduler(app, interval_hours=interval)


async def _register_job_handlers(app: FastAPI) -> None:
    """Import modules whose ``@job_handler`` decorators must run before the worker.

    importlib avoids the shadowing conflict between the local FastAPI ``app``
    parameter and the ``paper_ingestion`` package namespace.
    """
    import importlib  # noqa: PLC0415

    importlib.import_module("paper_ingestion.paper_jobs")
    importlib.import_module("paper_ingestion.extraction_jobs")
    importlib.import_module("paper_ingestion.contradiction_jobs")
    importlib.import_module("paper_ingestion.citations_jobs")
    importlib.import_module("paper_ingestion.pulse.job")
    importlib.import_module("paper_ingestion.pulse.training")
    importlib.import_module("paper_ingestion.integrations.zotero_service")


async def _shutdown_qdrant(app: FastAPI) -> None:
    """Close the Qdrant client before the http client tears down."""
    qdrant_client = getattr(app.state, "qdrant_client", None)
    if qdrant_client is not None:
        await qdrant_client.close()


async def _shutdown_scheduler(app: FastAPI) -> None:
    """Stop APScheduler without waiting for in-flight jobs."""
    scheduler = getattr(app.state, "scheduler", None)
    if scheduler is not None:
        scheduler.shutdown(wait=False)


_PAPER_INGESTION_JOB_KINDS: set[str] = {
    "pulse.generate",
    "pulse.train_classifier",
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
    "contradictions.scan",
    "digest.weekly",
    "zotero.push",
    "zotero.resync",
    "zotero.sync_from_zotero",
    "zotero.sync_annotations",
}


# ---------------------------------------------------------------------------
# App creation + middleware + error handlers
# ---------------------------------------------------------------------------

_lifespan_config = ServiceLifespanConfig(
    service_name="Paper Ingestion Service",
    http_client_kwargs={
        # paper_ingestion needs a longer read timeout (300s) than the shared
        # default (120s) because PDF downloads + extraction can run long.
        "timeout": httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0),
    },
    jobs_worker_kinds=_PAPER_INGESTION_JOB_KINDS,
    custom_init_tasks=[
        _validate_bbt_url_hook,
        _run_migrations_hook,
        _init_qdrant_and_pdf_pipeline,
        _init_source_singletons,
        _refresh_telegram_username,
        _start_scheduler_hook,
        _register_job_handlers,
    ],
    custom_teardown_tasks=[
        _shutdown_scheduler,
        _shutdown_qdrant,
    ],
)

app = FastAPI(
    title="JARVIS Paper Ingestion",
    description="Paper fetching, PDF processing, and embedding service",
    version="0.1.0",
    lifespan=configure_lifespan(_lifespan_config),
    dependencies=[Depends(verify_api_key)],
    default_response_class=DEFAULT_RESPONSE_CLASS,
)

configure_middleware_and_errors(
    app,
    limiter=limiter,
    trusted_proxy_hosts=get_core_settings().trusted_proxy_hosts_list,
)

# ---------------------------------------------------------------------------
# Router registration
# ---------------------------------------------------------------------------

from paper_ingestion.routers import (  # noqa: E402
    analytics,
    analyze,
    authors,
    citations,
    contradictions,
    dashboard_api,
    discovery,
    extractions,
    feed,
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
app.include_router(analytics.router)
app.include_router(snapshots.router)
app.include_router(authors.router)
app.include_router(citations.router)
app.include_router(contradictions.router)
app.include_router(extractions.router)
app.include_router(knowledge_graph.router)
app.include_router(dashboard_api.router)
app.include_router(analyze.router)
app.include_router(notes.router)
app.include_router(priority.router)
app.include_router(recommendations.router)
app.include_router(search.router)
app.include_router(discovery.router)
app.include_router(feed.router)
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
