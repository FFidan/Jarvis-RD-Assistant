"""Learning Engine Service - FastAPI application.

Thin entrypoint: lifespan, middleware, error handlers, health check,
and router registration.  Endpoint logic lives in ``learning_engine.routers.*`` modules.

Lifespan + middleware + error handlers are wired via
:func:`jarvis_common.configure_lifespan` /
:func:`jarvis_common.configure_middleware_and_errors` (DRY-002 -- the shared
factory).  Service-specific init (FSRS retention loading, CardGenerator,
AnkiExporter) lives in ``custom_init_tasks`` hooks below.
"""

import logging
from typing import Any

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse, ORJSONResponse
from jarvis_common import (
    ServiceLifespanConfig,
    configure_lifespan,
    configure_logging,
    configure_middleware_and_errors,
    maybe_init_sentry,
    register_health_routes,
    verify_api_key,
)
from jarvis_common.app_factory import (
    make_init_langfuse_hook,
    make_procrastinate_worker_hook,
)
from jarvis_common.app_factory import (
    shutdown_procrastinate_worker as shutdown_procrastinate_worker_common,
)
from jarvis_common.health import make_litellm_probe, make_postgres_probe
from jarvis_common.settings import get_core_settings
from jarvis_common.warmup import make_warmup_hook, warm_chat_model

from learning_engine.anki_exporter import AnkiExporter
from learning_engine.deps import limiter
from learning_engine.fsrs_manager import FSRSManager

configure_logging("learning_engine", log_level=get_core_settings().log_level)

maybe_init_sentry("learning_engine")

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
    """Construct AnkiExporter and default FSRSManager.

    CardGenerator is instantiated lazily inside ``generate_cards_core``; it no
    longer needs to be stashed on app.state.
    FSRSManager is built per-request on review paths (live DB retention values).
    """
    # Default FSRSManager used by card-creation paths (not reviews — reviews get per-request
    # manager with live DB values for desired_retention and learning_steps).
    app.state.fsrs_manager = FSRSManager()
    app.state.anki_exporter = AnkiExporter()


def _register_tasks(procrastinate_app: Any) -> None:
    """Bridge for ``make_procrastinate_worker_hook`` — registers learning_engine handlers.

    The service owns its kind→handler mapping (dependency inversion); the
    import stays deferred so task-handler modules load at lifespan start,
    not at module import.
    """
    from learning_engine._task_register import (  # noqa: PLC0415
        register_learning_engine_tasks,
    )

    register_learning_engine_tasks(procrastinate_app)


# B.4 Step 4 — start the procrastinate worker polling learning_engine + builtin
# (LOW-DRY1: hook body shared via jarvis_common.app_factory).
_start_procrastinate_worker = make_procrastinate_worker_hook(
    _register_tasks, queues=["learning_engine", "builtin"]
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
        _init_fsrs_and_generators,
        _start_procrastinate_worker,
        make_warmup_hook(lambda app: [lambda: warm_chat_model(app.state.http_client)]),
        _log_le_started,
    ],
    # Index-aligned with custom_init_tasks; None = no teardown counterpart.
    # Langfuse SDK auto-flushes on process exit — no explicit teardown needed.
    custom_teardown_tasks=[
        None,  # init_langfuse_hook
        None,  # _init_fsrs_and_generators
        _shutdown_procrastinate_worker,  # _start_procrastinate_worker
        None,  # make_warmup_hook (fire-and-forget; cancelled at process exit)
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

# SessionMiddleware populates request.state.user_id from the jarvis_session
# cookie issued by paper_ingestion's /api/auth/verify.  Sessions are shared
# across both services because both back onto the same Postgres `sessions`
# table.
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
    project_questions,
    projects,
    review,
    tasks,
)

app.include_router(projects.router)
app.include_router(tasks.router)
app.include_router(milestones.router)
app.include_router(analytics.router)
app.include_router(project_papers.router)
app.include_router(project_questions.router)
app.include_router(project_questions.questions_router)
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
    limiter=limiter,
)
