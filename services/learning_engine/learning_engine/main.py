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
from typing import Any

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse, ORJSONResponse
from jarvis_common import (
    ServiceLifespanConfig,
    build_database_url,
    configure_lifespan,
    configure_logging,
    configure_middleware_and_errors,
    register_health_routes,
    verify_api_key,
    warn_multitenant_stub,
)
from jarvis_common.app_factory import (
    make_init_langfuse_hook,
)
from jarvis_common.app_factory import (
    shutdown_procrastinate_worker as shutdown_procrastinate_worker_common,
)
from jarvis_common.health import make_litellm_probe, make_postgres_probe
from jarvis_common.settings import get_core_settings

from learning_engine.anki_exporter import AnkiExporter
from learning_engine.card_generator import CardGenerator
from learning_engine.deps import limiter
from learning_engine.fsrs_manager import FSRSManager

configure_logging("learning_engine", log_level=get_core_settings().log_level)
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


def _set_openai_client(openai_client: Any) -> None:
    """Bridge for ``make_init_langfuse_hook`` — populates ``learning_engine._state.svc``."""
    from learning_engine._state import set_services  # noqa: PLC0415

    set_services(openai_client=openai_client)


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

    Wires the procrastinate ``App`` connector to ``DATABASE_URL``, registers
    learning_engine task handlers (W4-1: dependency inversion — service owns
    its kind→handler mapping), and starts the worker polling the
    ``learning_engine`` + ``builtin`` queues.
    """
    from jarvis_common.task_registry import (  # noqa: PLC0415
        app as procrastinate_app,
    )
    from jarvis_common.task_registry import (
        set_dependencies,
    )
    from procrastinate.contrib.aiopg import AiopgConnector  # noqa: PLC0415

    from learning_engine._task_register import (  # noqa: PLC0415
        register_learning_engine_tasks,
    )

    # Register kind→handler mappings BEFORE the worker starts (W4-1).
    register_learning_engine_tasks(procrastinate_app)

    # Bind the connector to the same DSN the app_factory pool uses (reads from
    # /run/secrets/postgres_password; falls back to DATABASE_URL in tests).
    procrastinate_app.connector = AiopgConnector(dsn=build_database_url())
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
    await shutdown_procrastinate_worker_common(app)


async def _log_le_started(app: FastAPI) -> None:
    """Log service startup confirmation."""
    logger.info("Learning Engine init complete")


# ---------------------------------------------------------------------------
# App creation + middleware + error handlers
# ---------------------------------------------------------------------------

_lifespan_config = ServiceLifespanConfig(
    service_name="Learning Engine Service",
    custom_init_tasks=[
        make_init_langfuse_hook(_set_openai_client),
        warn_multitenant_stub,
        _init_fsrs_and_generators,
        _start_procrastinate_worker,
        _log_le_started,
    ],
    # Index-aligned with custom_init_tasks; None = no teardown counterpart.
    # Langfuse SDK auto-flushes on process exit — no explicit teardown needed.
    custom_teardown_tasks=[
        None,  # init_langfuse_hook
        None,  # warn_multitenant_stub
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

# WS-2A: SessionMiddleware populates request.state.user_id from the
# jarvis_session cookie issued by paper_ingestion's /api/auth/verify.
# Sessions are shared across both services because both back onto the
# same Postgres `sessions` table.
from jarvis_common.session_middleware import SessionMiddleware  # noqa: E402

app.add_middleware(SessionMiddleware)

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
# Health check — probes are service-owned; aggregator + routes live in
# jarvis_common.health (DOM-J-03).
# ---------------------------------------------------------------------------


register_health_routes(
    app,
    service_name="learning_engine",
    checks=[
        ("postgres", make_postgres_probe()),
        ("litellm", make_litellm_probe()),
    ],
)
