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
import json
import logging
import os
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, ORJSONResponse
from jarvis_common import (
    HealthCheckResponse,
    ServiceLifespanConfig,
    build_database_url,
    configure_lifespan,
    configure_logging,
    configure_middleware_and_errors,
    verify_api_key,
)
from jarvis_common.app_factory import (
    shutdown_procrastinate_worker as shutdown_procrastinate_worker_common,
)
from jarvis_common.llm_client import get_litellm_config
from jarvis_common.settings import get_core_settings
from jarvis_common.verify import QuoteVerifier
from qdrant_client import AsyncQdrantClient

# Trigger source registration via imports
import paper_ingestion.sources  # noqa: F401
from paper_ingestion.config import get_paper_ingestion_settings
from paper_ingestion.deps import limiter
from paper_ingestion.ingestion.embedder import Embedder
from paper_ingestion.integrations.zotero_client import validate_bbt_base_url
from paper_ingestion.migrations_runner import run_migrations
from paper_ingestion.models import PaperSourceConfig, SourceType
from paper_ingestion.pdf_processor import PDFProcessor
from paper_ingestion.services.telegram_bootstrap import refresh_telegram_bot_username
from paper_ingestion.sources.registry import get_source_class

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

configure_logging(
    "paper_ingestion", log_level=os.environ.get("LOG_LEVEL", "INFO")
)  # LOG_LEVEL not in Settings — intentionally left as-is (startup-only env read)
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


async def _init_langfuse_hook(app: FastAPI) -> None:
    """Initialize Langfuse SDK and Instructor-patched OpenAI client.

    No-op when LANGFUSE_HOST is unset — local dev without --profile observability.
    Attaches ``app.state.openai_client`` (and ``svc.openai_client``) for use by
    ``call_llm_structured`` in request handlers and job workers.
    """
    import instructor  # noqa: PLC0415
    import openai  # noqa: PLC0415
    from jarvis_common.llm_client import _langfuse_lifespan_hook  # noqa: PLC0415
    from jarvis_common.secrets import read_secret  # noqa: PLC0415

    from paper_ingestion._state import set_services  # noqa: PLC0415

    _langfuse_lifespan_hook()
    litellm_config = get_litellm_config()
    openai_client = instructor.from_openai(
        openai.AsyncOpenAI(
            base_url=f"{litellm_config.base_url}/v1",
            api_key=read_secret("LITELLM_MASTER_KEY") or "dummy",
        ),
        mode=instructor.Mode.JSON,
    )
    app.state.openai_client = openai_client
    set_services(openai_client=openai_client)


async def _warn_multitenant_stub(app: FastAPI) -> None:
    """C1 doc: log CRITICAL when MULTITENANT_ENABLED=true because auth resolver is a stub."""
    if get_paper_ingestion_settings().multitenant_enabled:
        logger.critical(
            "MULTITENANT_ENABLED=true but auth resolver is a stub — ownership checks are no-ops"
        )


async def _validate_bbt_url_hook(app: FastAPI) -> None:
    """PI-EDGE-008: validate ``BBT_BASE_URL`` to block file:// + unknown private IPs.

    Must run before any Zotero integration touches the URL — the SSRF surface
    area is the BBT translator endpoint.
    """
    validate_bbt_base_url()


async def _run_migrations_hook(app: FastAPI) -> None:
    """Apply DB migrations idempotently before any other init touches the schema."""
    await run_migrations(app.state.db_pool)


async def _migrate_plaintext_secrets_hook(app: FastAPI) -> None:
    """Eagerly re-encrypt any legacy plaintext secret rows in user_config.

    Runs after migrations so the schema is at the latest version. Best-effort:
    failures are logged inside the helper so this hook never blocks startup.
    """
    from paper_ingestion.routers.settings import (  # noqa: PLC0415
        migrate_plaintext_secrets,
    )

    try:
        await migrate_plaintext_secrets(app.state.db_pool)
    except Exception:
        logger.warning(
            "migrate_plaintext_secrets failed during startup — continuing",
            exc_info=True,
        )


async def _init_qdrant_and_pdf_pipeline(app: FastAPI) -> None:
    """Construct Qdrant client + Embedder + PDFProcessor + QuoteVerifier."""
    _cfg = get_paper_ingestion_settings()
    qdrant_url = _cfg.qdrant_url
    qdrant_api_key = _cfg.qdrant_api_key.get_secret_value() if _cfg.qdrant_api_key else None
    app.state.qdrant_client = AsyncQdrantClient(
        url=qdrant_url, api_key=qdrant_api_key, check_compatibility=False
    )
    app.state.embedder = Embedder(app.state.http_client, app.state.qdrant_client)
    await app.state.embedder.ensure_collection()
    app.state.pdf_processor = PDFProcessor(app.state.http_client, app.state.embedder)
    app.state.verifier = QuoteVerifier()

    # Populate module-level service state so job handlers can access these
    # objects without importing paper_ingestion.main (which would be circular).
    from paper_ingestion._state import set_services  # noqa: PLC0415

    set_services(
        pdf_processor=app.state.pdf_processor,
        embedder=app.state.embedder,
        verifier=app.state.verifier,
    )


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
    from paper_ingestion._state import set_services  # noqa: PLC0415

    set_services(sources=app.state.sources)


async def _refresh_telegram_username(app: FastAPI) -> None:
    """Refresh cached Telegram bot username for setup-wizard pairing links."""
    await refresh_telegram_bot_username(app.state.db_pool, app.state.http_client)


async def _rehydrate_litellm_aliases(pool: Any) -> None:
    """Push user_config LLM model choices into LiteLLM on every boot.

    On restart LiteLLM reloads its :ro YAML which may not match what the user
    selected in Settings.  This re-applies any stored llm.* keys so the proxy
    reflects the user's choices without requiring a docker restart.
    """
    from paper_ingestion.services.litellm_config import update_litellm_model  # noqa: PLC0415

    for config_key in ("llm.smart_model", "llm.fast_model", "llm.embed_model"):
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT value FROM user_config WHERE key = $1", config_key)
        if row is None:
            continue
        model_id: str = row["value"]
        try:
            await update_litellm_model(config_key, model_id)
        except RuntimeError as exc:
            if "HTTP 400" in str(exc) and "No DB Connected" in str(exc):
                logger.info(
                    "LiteLLM has no admin DB attached; skipping %s rehydration "
                    "(this is expected when STORE_MODEL_IN_DB is unset).",
                    config_key,
                )
                continue
            raise


async def _autoconfigure_models_hook(app: FastAPI) -> None:
    """On first boot: detect hardware tier and write best-fit models to user_config."""
    from paper_ingestion.services.litellm_config import ROLE_TO_ALIAS  # noqa: PLC0415
    from paper_ingestion.services.model_lifecycle import (  # noqa: PLC0415
        detect_hardware,
        recommendations_for_role,
    )

    pool = app.state.db_pool
    await _rehydrate_litellm_aliases(pool)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT value FROM user_config WHERE key = 'system.models_autoconfigured'"
        )
    if row is not None:
        return  # Already ran; rehydrate above is all we need

    hardware = detect_hardware()
    logger.info(
        "First-boot auto-configure: hardware tier=%d vram=%.1f GB",
        hardware.tier,
        hardware.vram_gb,
    )
    role_key_pairs = [
        ("smart", "llm.smart_model"),
        ("fast", "llm.fast_model"),
        ("embed", "llm.embed_model"),
    ]
    for role, config_key in role_key_pairs:
        if config_key not in ROLE_TO_ALIAS:
            continue
        recs = recommendations_for_role(
            role,
            installed=[],
            current={},
            embedding_model_name="",
            hardware=hardware,
            cloud_api_keys={},
        )
        best = next((r for r in recs if r["status"] not in ("unfit",)), None)
        if best is None:
            continue
        model_id: str = best["id"]
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO user_config (key, value) VALUES ($1, $2::jsonb) "
                "ON CONFLICT (key) DO NOTHING",
                config_key,
                json.dumps(model_id),
            )
        logger.info("Auto-configured %s → %s (tier %d)", config_key, model_id, best["tier"])

    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO user_config (key, value) "
            "VALUES ('system.models_autoconfigured', 'true'::jsonb) "
            "ON CONFLICT (key) DO NOTHING"
        )
    await _rehydrate_litellm_aliases(pool)


async def _start_scheduler_hook(app: FastAPI) -> None:
    """Always start the scheduler so live toggles take effect without restart."""
    interval = get_paper_ingestion_settings().auto_fetch_interval_hours
    from .scheduler import start_scheduler  # noqa: PLC0415

    app.state.scheduler = await start_scheduler(app, interval_hours=interval)


async def _start_procrastinate_worker(app: FastAPI) -> None:
    """B.4 Step 4 — start the procrastinate worker (legacy worker removed).

    Wires the procrastinate ``App`` connector with ``DATABASE_URL`` (the same
    DSN backing ``app.state.db_pool``), registers paper_ingestion task handlers
    (W4-1: dependency inversion — service owns its kind→handler mapping), opens
    the connector, threads ``(pool, http_client)`` into ``task_registry`` so
    task wrappers can access the shared singletons, then starts the worker as
    a background asyncio task. Stored on ``app.state`` for the symmetric
    teardown.
    """
    from jarvis_common.task_registry import (  # noqa: PLC0415
        app as procrastinate_app,
    )
    from jarvis_common.task_registry import (
        set_dependencies,
    )
    from procrastinate.contrib.aiopg import AiopgConnector  # noqa: PLC0415

    from paper_ingestion._task_register import (  # noqa: PLC0415
        register_paper_ingestion_tasks,
    )

    # Register kind→handler mappings BEFORE the worker starts (W4-1).
    register_paper_ingestion_tasks(procrastinate_app)

    # The connector built at task_registry import time has no DSN — replace it
    # with one bound to the same DSN the app_factory pool uses (reads from
    # /run/secrets/postgres_password; falls back to DATABASE_URL in tests).
    procrastinate_app.connector = AiopgConnector(dsn=build_database_url())
    procrastinate_app.job_manager.connector = procrastinate_app.connector
    await procrastinate_app.open_async()

    set_dependencies(app.state.db_pool, app.state.http_client)

    app.state.procrastinate_app = procrastinate_app
    app.state.procrastinate_worker_task = asyncio.create_task(
        procrastinate_app.run_worker_async(
            queues=["paper_ingestion", "builtin"],
            install_signal_handlers=False,
        ),
        name="procrastinate_worker",
    )


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


async def _shutdown_procrastinate_worker(app: FastAPI) -> None:
    """Cancel the procrastinate worker task and close the connector."""
    await shutdown_procrastinate_worker_common(app)


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
    custom_init_tasks=[
        _init_langfuse_hook,
        _warn_multitenant_stub,
        _validate_bbt_url_hook,
        _run_migrations_hook,
        _migrate_plaintext_secrets_hook,
        _init_qdrant_and_pdf_pipeline,
        _init_source_singletons,
        _refresh_telegram_username,
        _autoconfigure_models_hook,
        _start_scheduler_hook,
        _start_procrastinate_worker,
    ],
    # Index-aligned with custom_init_tasks; None = no teardown counterpart.
    custom_teardown_tasks=[
        None,  # _init_langfuse_hook (Langfuse SDK auto-flushes on process exit)
        None,  # _warn_multitenant_stub
        None,  # _validate_bbt_url_hook
        None,  # _run_migrations_hook
        None,  # _migrate_plaintext_secrets_hook
        _shutdown_qdrant,  # _init_qdrant_and_pdf_pipeline
        None,  # _init_source_singletons
        None,  # _refresh_telegram_username
        None,  # _autoconfigure_models_hook
        _shutdown_scheduler,  # _start_scheduler_hook
        _shutdown_procrastinate_worker,  # _start_procrastinate_worker
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

# WS-2A: SessionMiddleware reads the jarvis_session cookie and populates
# request.state.user_id. Added AFTER configure_middleware_and_errors so it
# sits inside (i.e. runs AFTER) RequestIDMiddleware/SlowAPI/CORS/ProxyHeaders
# in the request flow — by then the request has its public scheme/host
# resolved and a request-id attached, which makes session lookup events
# correlatable in logs.
from jarvis_common.session_middleware import SessionMiddleware  # noqa: E402

app.add_middleware(SessionMiddleware)

# ---------------------------------------------------------------------------
# Router registration
# ---------------------------------------------------------------------------

from paper_ingestion.routers import admin as admin_router  # noqa: E402
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
    infra_events,
    jobs,
    knowledge_graph,
    logs,
    my_day,
    notes,
    papers,
    pdf,
    priority,
    rag,
    recommendation_feedback,
    recommendations,
    search,
    settings,
    snapshots,
    system,
    telegram,
    topics,
)
from paper_ingestion.routers import auth as auth_router  # noqa: E402
from paper_ingestion.routers import pulse as pulse_router  # noqa: E402
from paper_ingestion.routers import setup as setup_router  # noqa: E402
from paper_ingestion.routers import zotero as zotero_router  # noqa: E402

app.include_router(auth_router.router)
# WS-2B: admin router uses session-only auth (no X-API-Key required for browser
# sessions). Exempt from the global verify_api_key dep via dependencies=[].
app.include_router(admin_router.router, dependencies=[])
# WS-2F: setup router is the first-run bootstrap. Endpoints are wide open until
# the first admin exists; afterwards each handler enforces admin-role itself
# via require_unconfigured_or_admin. Exempt from global verify_api_key.
app.include_router(setup_router.router, dependencies=[])
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
app.include_router(my_day.router)
app.include_router(notes.router)
app.include_router(priority.router)
app.include_router(recommendations.router)
app.include_router(recommendation_feedback.router)
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
app.include_router(logs.router)
app.include_router(infra_events.router)


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
