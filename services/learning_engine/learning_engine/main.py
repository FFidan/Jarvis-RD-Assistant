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


async def _init_langfuse_hook(app: FastAPI) -> None:
    """Initialize Langfuse SDK and Instructor-patched OpenAI client.

    No-op when LANGFUSE_HOST is unset — local dev without --profile observability.
    Attaches ``app.state.openai_client`` (and ``svc.openai_client``) for use by
    ``call_llm_structured`` in ``CardGenerator.generate_cards``.
    """
    import instructor  # noqa: PLC0415
    import openai  # noqa: PLC0415
    from jarvis_common.llm_client import (  # noqa: PLC0415
        _langfuse_lifespan_hook,
        get_litellm_config,
    )

    from learning_engine._state import svc  # noqa: PLC0415

    _langfuse_lifespan_hook()
    litellm_config = get_litellm_config()
    openai_client = instructor.from_openai(
        openai.AsyncOpenAI(
            base_url=f"{litellm_config.base_url}/v1",
            api_key=os.environ.get("LITELLM_MASTER_KEY") or "dummy",
        ),
        mode=instructor.Mode.JSON,
    )
    app.state.openai_client = openai_client
    svc.openai_client = openai_client


async def _warn_multitenant_stub(app: FastAPI) -> None:
    """C1 doc: log CRITICAL when MULTITENANT_ENABLED=true because auth resolver is a stub."""
    if os.getenv("MULTITENANT_ENABLED", "false").lower() == "true":
        logger.critical(
            "MULTITENANT_ENABLED=true but auth resolver is a stub — ownership checks are no-ops"
        )


async def _init_fsrs_and_generators(app: FastAPI) -> None:
    """Construct CardGenerator + AnkiExporter. FSRSManager is built per-review from DB."""
    from jarvis_common.llm_client import get_litellm_config

    litellm_config = get_litellm_config()
    # Default FSRSManager used by card-creation paths (not reviews — reviews get per-request
    # manager with live DB values for desired_retention and learning_steps).
    app.state.fsrs_manager = FSRSManager()
    app.state.card_generator = CardGenerator(
        http_client=app.state.http_client,
        litellm_config=litellm_config,
    )
    app.state.anki_exporter = AnkiExporter()


async def _start_procrastinate_worker(app: FastAPI) -> None:
    """B.4 Step 4 — start the procrastinate worker (legacy worker removed).

    Wires the procrastinate ``App`` connector to ``DATABASE_URL`` and starts the
    worker polling the ``learning_engine`` + ``builtin`` queues.
    """
    from jarvis_common.task_registry import (  # noqa: PLC0415
        app as procrastinate_app,
    )
    from jarvis_common.task_registry import (
        set_dependencies,
    )
    from procrastinate.contrib.aiopg import AiopgConnector  # noqa: PLC0415

    # Bind the connector to the same DSN backing app.state.db_pool — the
    # task_registry-time connector has no DSN.
    procrastinate_app.connector = AiopgConnector(dsn=os.environ["DATABASE_URL"])
    procrastinate_app.job_manager.connector = procrastinate_app.connector
    await procrastinate_app.open_async()

    set_dependencies(app.state.db_pool, app.state.http_client)

    app.state.procrastinate_app = procrastinate_app
    app.state.procrastinate_worker_task = asyncio.create_task(
        procrastinate_app.run_worker_async(
            queues=["learning_engine", "builtin"],
            install_signal_handlers=False,
        ),
        name="procrastinate_worker",
    )


async def _shutdown_procrastinate_worker(app: FastAPI) -> None:
    """Cancel the procrastinate worker task and close the connector."""
    import contextlib  # noqa: PLC0415

    task = getattr(app.state, "procrastinate_worker_task", None)
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    procrastinate_app = getattr(app.state, "procrastinate_app", None)
    if procrastinate_app is not None:
        with contextlib.suppress(Exception):
            await procrastinate_app.close_async()


async def _log_le_started(app: FastAPI) -> None:
    """Log service startup confirmation."""
    logger.info("Learning Engine init complete")


# ---------------------------------------------------------------------------
# App creation + middleware + error handlers
# ---------------------------------------------------------------------------

_lifespan_config = ServiceLifespanConfig(
    service_name="Learning Engine Service",
    custom_init_tasks=[
        _init_langfuse_hook,
        _warn_multitenant_stub,
        _init_fsrs_and_generators,
        _start_procrastinate_worker,
        _log_le_started,
    ],
    # Index-aligned with custom_init_tasks; None = no teardown counterpart.
    # Langfuse SDK auto-flushes on process exit — no explicit teardown needed.
    custom_teardown_tasks=[
        None,  # _init_langfuse_hook
        None,  # _warn_multitenant_stub
        None,  # _init_fsrs_and_generators
        _shutdown_procrastinate_worker,  # _start_procrastinate_worker
        None,  # _log_le_started
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
    executive_intent,
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
app.include_router(executive_intent.router)
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
