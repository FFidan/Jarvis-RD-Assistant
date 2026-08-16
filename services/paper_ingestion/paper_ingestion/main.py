"""Paper Ingestion Service - FastAPI application.

Thin entrypoint: lifespan, middleware, error handlers, health check,
and router registration.  Endpoint logic lives in
``paper_ingestion.routers.*`` modules.

Extracted modules
-----------------
* ``jarvis_common.migrations`` — ``run_migrations()``
* ``paper_ingestion.routers.system`` — ``GET /api/system/models``

Lifespan + middleware + error handlers are wired via
:func:`jarvis_common.configure_lifespan` /
:func:`jarvis_common.configure_middleware_and_errors` (DRY-002 -- the shared
factory keeps both microservices in lockstep on the cross-cutting concerns
while letting paper_ingestion express its rich init pipeline as
``custom_init_tasks`` hooks).
"""

import asyncio
import contextlib
import dataclasses
import logging
import socket
from collections.abc import Mapping, Sequence
from functools import partial
from typing import Any

import asyncpg
import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
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
    make_maintenance_watcher_hook,
    make_procrastinate_worker_hook,
    shutdown_maintenance_watcher,
)
from jarvis_common.app_factory import (
    shutdown_procrastinate_worker as shutdown_procrastinate_worker_common,
)
from jarvis_common.auth import validate_runtime_config
from jarvis_common.cached_transport import CachingTransport
from jarvis_common.config import get_jarvis_common_settings
from jarvis_common.health import make_litellm_probe, make_postgres_probe
from jarvis_common.identity_capabilities import required_identity_scopes
from jarvis_common.identity_keys import load_identity_verifier_from_settings
from jarvis_common.identity_middleware import IdentityAssertionMiddleware
from jarvis_common.migrations import run_migrations
from jarvis_common.pinned_transport import JARVIS_SERVICE_POLICY, PinnedAsyncTransport
from jarvis_common.settings import get_core_settings, get_secrets_settings
from jarvis_common.verify import QuoteVerifier
from jarvis_common.version import app_version
from jarvis_common.warmup import make_warmup_hook, warm_chat_model, warm_embedding_model
from qdrant_client import AsyncQdrantClient

# Trigger source registration via imports
import paper_ingestion.sources  # noqa: F401
from paper_ingestion.config import get_paper_ingestion_settings
from paper_ingestion.deps import limiter
from paper_ingestion.ingestion.embedder import Embedder
from paper_ingestion.ingestion.embedding_config import (
    COLLECTION_NAME,
    EMBEDDING_MODEL,
    EMBEDDING_MODEL_NAME,
)
from paper_ingestion.ingestion.payload_schema import run_visibility_reconciler
from paper_ingestion.integrations.zotero_client import (
    BBT_ALLOWED_PRIVATE_HOSTS_CONFIG_KEY,
    BBTPrivateHostError,
    refresh_configured_private_hosts,
    validate_bbt_base_url,
)
from paper_ingestion.litellm_reconciler import (
    _shutdown_litellm_reconciler,
    _start_litellm_reconciler,
)
from paper_ingestion.models import PaperSourceConfig, SourceType
from paper_ingestion.pdf_processor import PDFProcessor
from paper_ingestion.services.model_lifecycle import (
    catalog_entry_for_model,
    detect_hardware,
    fits_with_embed_reserve,
    recommendations_for_role,
    safe_num_ctx,
)
from paper_ingestion.services.source_helper import _decrypt_config_api_key
from paper_ingestion.sources.registry import get_source_class

configure_logging("paper_ingestion", log_level=get_core_settings().log_level)
maybe_init_sentry("paper_ingestion")

logger = logging.getLogger(__name__)

_identity_settings = get_jarvis_common_settings()


# ---------------------------------------------------------------------------
# Lifespan custom hooks (DRY-002 — orchestrated by jarvis_common.app_factory)
# ---------------------------------------------------------------------------


def _set_openai_client(openai_client: Any) -> None:
    """Bridge for ``make_init_langfuse_hook`` — populates ``paper_ingestion._state.svc``."""
    from paper_ingestion._state import set_services  # noqa: PLC0415

    set_services(openai_client=openai_client)


async def _validate_bbt_url_hook(app: FastAPI) -> None:
    """Validate ``BBT_BASE_URL`` to block file:// + unknown private IPs.

    Must run before any Zotero integration touches the URL — the SSRF surface
    area is the BBT translator endpoint.

    A private host is reported, not fatal: aborting the boot would lock the
    operator out of the Settings page where the host is allowlisted, and the
    Better BibTeX request path enforces the same policy per request. An
    unsupported scheme is a configuration typo and still stops startup.
    """
    await refresh_configured_private_hosts(app.state.db_pool)
    try:
        validate_bbt_base_url()
    except BBTPrivateHostError as exc:
        logger.warning(
            "%s Add the host under %s in Settings to allow it.",
            exc,
            BBT_ALLOWED_PRIVATE_HOSTS_CONFIG_KEY,
        )


async def _run_migrations_hook(app: FastAPI) -> None:
    """Apply DB migrations idempotently before any other init touches the schema."""
    await run_migrations(app.state.db_pool)


async def _validate_runtime_config_hook(app: FastAPI) -> None:
    """Post-migration boot gate keyed on live DB state — fail loudly on a
    multi-user box without a real Pulse HMAC key, a production box with no admin
    and no setup token, or a multi-user production box with no deliverable SMTP.

    Runs immediately after migrations so ``users``/``user_config`` exist; a fresh
    pre-migration DB is skipped defensively (mirrors validate_encrypted_config_rows).
    """
    secrets = get_secrets_settings()
    hmac_key = secrets.jarvis_model_hmac_key
    model_hmac_ok = hmac_key is not None and len(hmac_key.get_secret_value()) >= 32
    setup_token = secrets.jarvis_setup_token
    setup_token_set = setup_token is not None and bool(setup_token.get_secret_value().strip())
    try:
        await validate_runtime_config(
            app.state.db_pool,
            environment=get_core_settings().environment,
            setup_token_set=setup_token_set,
            model_hmac_ok=model_hmac_ok,
        )
    except (asyncpg.UndefinedTableError, asyncpg.UndefinedColumnError):
        logger.warning("validate_runtime_config skipped: schema not yet available (fresh DB)")


async def _run_hw_probe_hook(app: FastAPI) -> None:
    """Re-probe hardware tier at boot and record any tier change to system_events."""
    from paper_ingestion.hw_probe import run_boot_probe  # noqa: PLC0415

    await run_boot_probe(app.state.db_pool)


async def _init_qdrant_and_pdf_pipeline(app: FastAPI) -> None:
    """Initialize Qdrant, visibility repair, PDF processing, and verification."""
    _cfg = get_paper_ingestion_settings()
    qdrant_url = _cfg.qdrant_url
    qdrant_api_key = _cfg.qdrant_api_key.get_secret_value() if _cfg.qdrant_api_key else None
    app.state.qdrant_client = AsyncQdrantClient(
        url=qdrant_url, api_key=qdrant_api_key, check_compatibility=False
    )
    app.state.embedder = Embedder(
        app.state.http_client,
        app.state.qdrant_client,
        db_pool=app.state.db_pool,
    )
    await app.state.embedder.ensure_collection()
    app.state.vector_visibility_task = asyncio.create_task(
        run_visibility_reconciler(app.state.db_pool, app.state.embedder),
        name="vector_visibility_reconciler",
    )
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
    """Initialize source singletons so rate-limiter state persists across requests."""
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
                    config=_decrypt_config_api_key(row["config"] or {}),
                )
                app.state.sources[source_type_val] = source_cls(
                    config, app.state.http_client, db_pool=app.state.db_pool
                )
        except Exception:
            logger.warning(
                "Could not initialize source singleton for %s",
                source_type_val,
                exc_info=True,
            )

    # Expose sources through module-level state so the pulse job can reach it.
    from paper_ingestion._state import set_services  # noqa: PLC0415

    set_services(sources=app.state.sources)


# Keep below jarvis_common.health._PROBE_TIMEOUT_S so Qdrant metadata slowness
# is classified by _probe_qdrant as "unknown", not by the outer sweep as
# degraded "timeout".
_QDRANT_HEALTH_TIMEOUT_S = 4.0


async def _fetch_installed_ollama_models(app: FastAPI) -> list[dict[str, Any]]:
    """Best-effort Ollama ``/api/tags`` listing; ``[]`` when unreachable.

    The hook runs post-``service_completed_successfully`` so the bootstrap
    models are normally pulled already; a fetch failure must not crash boot —
    the empty list falls back to catalog-only (smallest-first) seeding.
    """
    try:
        ollama_url = str(get_paper_ingestion_settings().ollama_base_url).rstrip("/")
        resp = await app.state.http_client.get(f"{ollama_url}/api/tags", timeout=10.0)
        resp.raise_for_status()
        models = resp.json().get("models", [])
        return list(models) if isinstance(models, list) else []
    except Exception:
        logger.warning(
            "Auto-configure: could not list installed Ollama models; "
            "falling back to catalog-only seeding",
            exc_info=True,
        )
        return []


def _choose_autoconfigured_model(
    recommendations: Sequence[Mapping[str, Any]],
    hardware: Any,
    embed_reserve_gb: float,
) -> Mapping[str, Any] | None:
    """Pick the largest installed model that fits beside the embedder.

    Among installed entries (status ``pulled``/``active``) the chooser prefers
    the highest catalog tier, then the highest catalog VRAM requirement —
    qwen3:4b and qwen3:8b are both tier 1, so the VRAM tie-break is what
    prefers 8b. Eligibility is ``fits_with_embed_reserve`` at the catalog
    default num_ctx. On CPU / failed probe (``vram_gb == 0.0``), or when
    nothing installed is eligible, keep the legacy smallest-first pick over
    ``recommendations_for_role``'s ascending sort (the sort itself is
    wire-shared and stays untouched).
    """
    if hardware.vram_gb > 0.0:
        eligible: list[Mapping[str, Any]] = []
        for item in recommendations:
            if item["status"] not in ("pulled", "active"):
                continue
            entry = catalog_entry_for_model(str(item["id"]))
            if entry is not None and fits_with_embed_reserve(entry, hardware, embed_reserve_gb):
                eligible.append(item)
        if eligible:
            return max(eligible, key=lambda item: (int(item["tier"]), float(item["vram_gb"])))
    # Smallest-first fallback serving BOTH the CPU carve-out (vram_gb == 0.0) and
    # the GPU-but-nothing-installed-eligible case: take the first non-unfit rec.
    return next((r for r in recommendations if r["status"] not in ("unfit",)), None)


async def _autoconfigure_models_hook(app: FastAPI) -> None:
    """On first boot: seed best-fit installed models + a safe num_ctx per role.

    Delivery to LiteLLM is NOT done here — the reconciler started right after
    this hook (``_start_litellm_reconciler``) reads the rows written below and
    keeps retrying until LiteLLM accepts the deployments. The per-machine
    ``llm.<machine>.<role>_num_ctx`` row seeded alongside the model is picked
    up by that same delivery (``update_litellm_model`` → ``_get_num_ctx``).
    """
    from jarvis_common.config_metadata import ROLE_TO_ALIAS  # noqa: PLC0415

    pool = app.state.db_pool

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT value FROM user_config "
            "WHERE key = 'system.models_autoconfigured' AND user_id IS NULL"
        )
    if row is not None:
        return  # Already ran; the reconciler handles delivery

    installed = await _fetch_installed_ollama_models(app)
    hardware = await asyncio.to_thread(detect_hardware)
    logger.info(
        "First-boot auto-configure: hardware tier=%d vram=%.1f GB, %d installed models",
        hardware.tier,
        hardware.vram_gb,
        len(installed),
    )
    # GPU-overlay divergence (JARVIS_HOST_VRAM_MB set but the in-container probe
    # sees no GPU) means the stack's ollama almost certainly has no GPU either:
    # seeding the largest model + a big num_ctx would act on phantom VRAM while
    # the UI simultaneously warns "GPU overlay not active". Seed conservatively
    # off a vram=0 view — the same smallest-first carve-out + catalog-default
    # num_ctx the CPU path uses. detect_hardware itself is left unchanged so the
    # API keeps reporting the host value + the divergence flag the UI warns on.
    if hardware.host_gpu_divergence:
        seed_hardware = dataclasses.replace(
            hardware, vram_gb=0.0, tier=0, host_gpu_divergence=False
        )
    else:
        seed_hardware = hardware
    # Only smart + fast are auto-configured. The embed model is dimension-locked
    # to the Qdrant collection (EMBEDDING_MODEL_NAME / qwen3-embedding:4b): the
    # tier recommender can pick a different embedder (e.g. mxbai-embed-large) that
    # is neither pulled nor dimension-compatible, which would silently break
    # embeddings. Leaving llm.embed_model unset keeps the LiteLLM `embed` alias on
    # its pulled static default; switching embedders is a deliberate re-embed op.
    # It does, however, occupy VRAM permanently — hence the embed reserve below.
    embed_entry = catalog_entry_for_model(EMBEDDING_MODEL_NAME)
    embed_reserve_gb = embed_entry.vram_gb if embed_entry is not None else 0.0
    machine_id = hardware.machine_id or socket.gethostname()
    role_key_pairs = [
        ("smart", "llm.smart_model"),
        ("fast", "llm.fast_model"),
    ]
    for role, config_key in role_key_pairs:
        if config_key not in ROLE_TO_ALIAS:
            continue
        recs = recommendations_for_role(
            role,
            installed=installed,
            current={},
            embedding_model_name="",
            hardware=seed_hardware,
            cloud_api_keys={},
        )
        best = _choose_autoconfigured_model(recs, seed_hardware, embed_reserve_gb)
        if best is None:
            continue
        model_id: str = best["id"]
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO user_config (key, value) VALUES ($1, $2::jsonb) "
                "ON CONFLICT (user_id, key) DO NOTHING",
                config_key,
                model_id,
            )
        num_ctx: int | None = None
        entry = catalog_entry_for_model(model_id)
        if entry is not None and entry.provider == "ollama":
            num_ctx = safe_num_ctx(entry, seed_hardware, embed_reserve_gb)
            async with pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO user_config (key, value) VALUES ($1, $2::jsonb) "
                    "ON CONFLICT (user_id, key) DO NOTHING",
                    f"llm.{machine_id}.{ROLE_TO_ALIAS[config_key]}_num_ctx",
                    num_ctx,
                )
        logger.info(
            "Auto-configured %s → %s (tier %d, num_ctx %s)",
            config_key,
            model_id,
            best["tier"],
            num_ctx,
        )

    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO user_config (key, value) "
            "VALUES ('system.models_autoconfigured', 'true'::jsonb) "
            "ON CONFLICT (user_id, key) DO NOTHING"
        )


async def _start_scheduler_hook(app: FastAPI) -> None:
    """Always start the scheduler so live toggles take effect without restart."""
    interval = get_paper_ingestion_settings().auto_fetch_interval_hours
    from .scheduler import start_scheduler  # noqa: PLC0415

    app.state.scheduler = await start_scheduler(app, interval_hours=interval)


def _register_tasks(procrastinate_app: Any) -> None:
    """Bridge for ``make_procrastinate_worker_hook`` — registers paper_ingestion handlers.

    The service owns its kind→handler mapping (dependency inversion); the
    import stays deferred so task-handler modules load at lifespan start,
    not at module import.
    """
    from paper_ingestion._task_register import (  # noqa: PLC0415
        register_paper_ingestion_tasks,
    )

    register_paper_ingestion_tasks(procrastinate_app)


# The worker and maintenance watcher share one queue list so pause and resume
# always cover the queues the service polls.
_WORKER_QUEUES = ["paper_ingestion", "builtin"]
_start_procrastinate_worker = make_procrastinate_worker_hook(_register_tasks, queues=_WORKER_QUEUES)
_maintenance_watcher = make_maintenance_watcher_hook(queues=_WORKER_QUEUES)


async def _prune_job_history_hook(app: FastAPI) -> None:
    """Prune job history once at boot so a fresh install does not wait for the daily run."""
    from .scheduler import purge_job_history_task  # noqa: PLC0415

    await purge_job_history_task(app)


async def _shutdown_qdrant(app: FastAPI) -> None:
    """Cancel visibility reconciliation, then close Qdrant cleanly."""
    visibility_task = getattr(app.state, "vector_visibility_task", None)
    if visibility_task is not None:
        visibility_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await visibility_task
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


async def _load_identity_verifier_hook(app: FastAPI) -> None:
    """Load the Research verifier before domain-specific startup hooks run.

    Parameters
    ----------
    app : FastAPI
        Research application whose state receives the audience-bound verifier.
    """
    if not _identity_settings.identity_assertions_required:
        return
    app.state.identity_verifier = load_identity_verifier_from_settings(
        _identity_settings,
        audience="research",
    )


# ---------------------------------------------------------------------------
# App creation + middleware + error handlers
# ---------------------------------------------------------------------------

_lifespan_config = ServiceLifespanConfig(
    service_name="Paper Ingestion Service",
    http_client_kwargs={
        # paper_ingestion needs a longer read timeout (300s) than the shared
        # default (120s) because PDF downloads + extraction can run long.
        "timeout": httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0),
        "transport": CachingTransport(PinnedAsyncTransport(JARVIS_SERVICE_POLICY)),
    },
    custom_init_tasks=[
        _load_identity_verifier_hook,
        make_init_langfuse_hook(_set_openai_client),
        _validate_bbt_url_hook,
        _run_migrations_hook,
        _validate_runtime_config_hook,
        _run_hw_probe_hook,
        _init_qdrant_and_pdf_pipeline,
        _init_source_singletons,
        _autoconfigure_models_hook,
        _start_litellm_reconciler,
        _start_scheduler_hook,
        _start_procrastinate_worker,
        _prune_job_history_hook,
        _maintenance_watcher,
        make_warmup_hook(
            lambda app: [
                lambda: warm_embedding_model(app.state.http_client, EMBEDDING_MODEL),
                lambda: warm_chat_model(app.state.http_client),
            ]
        ),
    ],
    # Index-aligned with custom_init_tasks; None = no teardown counterpart.
    custom_teardown_tasks=[
        None,  # _load_identity_verifier_hook
        None,  # init_langfuse_hook (Langfuse SDK auto-flushes on process exit)
        None,  # _validate_bbt_url_hook
        None,  # _run_migrations_hook
        None,  # _validate_runtime_config_hook
        None,  # _run_hw_probe_hook
        _shutdown_qdrant,  # _init_qdrant_and_pdf_pipeline
        None,  # _init_source_singletons
        None,  # _autoconfigure_models_hook
        _shutdown_litellm_reconciler,  # _start_litellm_reconciler
        _shutdown_scheduler,  # _start_scheduler_hook
        _shutdown_procrastinate_worker,  # _start_procrastinate_worker
        None,  # _prune_job_history_hook
        # Shutdown runs in reverse order, so the watcher stops before the worker
        # connector closes.
        shutdown_maintenance_watcher,  # _maintenance_watcher
        None,  # make_warmup_hook has no cleanup callback
    ],
)

app = FastAPI(
    title="JARVIS Paper Ingestion",
    description="Paper fetching, PDF processing, and embedding service",
    version=app_version(),
    lifespan=configure_lifespan(_lifespan_config),
    dependencies=[Depends(verify_api_key)],
)

configure_middleware_and_errors(
    app,
    limiter=limiter,
    trusted_proxy_hosts=get_core_settings().trusted_proxy_hosts_list,
)

# Qdrant outage on non-stream Ask routes → 503 (stream routes handle it themselves
# via a degraded SSE frame, since a started SSE can't change its HTTP status code).
from paper_ingestion.rag.exceptions import QdrantUnavailableError  # noqa: E402


@app.exception_handler(QdrantUnavailableError)
async def _qdrant_unavailable_handler(
    request: Request, exc: QdrantUnavailableError
) -> JSONResponse:
    logger.warning("Qdrant unavailable: %s", exc)
    return JSONResponse(
        status_code=503,
        content={"detail": "Vector search is temporarily unavailable. Please try again later."},
    )


# SessionMiddleware reads the jarvis_session cookie and populates
# request.state.user_id. Added AFTER configure_middleware_and_errors so it
# sits OUTSIDE (i.e. runs BEFORE) RequestIDMiddleware/SlowAPI/CORS/ProxyHeaders
# in the request flow — Starlette add_middleware prepends, so the last-added
# middleware is outermost and executes first. This means request.state.user_id
# is already set when SlowAPI's rate-limit key function runs, which is what
# makes the per-user rate-limit guarantee hold.
from jarvis_common.session_middleware import SessionMiddleware  # noqa: E402

app.add_middleware(SessionMiddleware)
if _identity_settings.identity_assertions_required:
    app.add_middleware(
        IdentityAssertionMiddleware,
        scope_resolver=partial(required_identity_scopes, "research"),
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
    highlights,
    infra_events,
    internal_config,
    jobs,
    knowledge_graph,
    my_day,
    notes,
    papers,
    pdf_actions,
    pdf_files,
    priority,
    rag,
    recommendation_feedback,
    recommendations,
    search,
    settings,
    snapshots,
    system,
    system_capabilities,
    system_readiness,
    threads,
    topics,
)
from paper_ingestion.routers import backups as backups_router  # noqa: E402
from paper_ingestion.routers import pulse as pulse_router  # noqa: E402
from paper_ingestion.routers import settings_ai as settings_ai_router  # noqa: E402
from paper_ingestion.routers import source_config as source_config_router  # noqa: E402
from paper_ingestion.routers import zotero as zotero_router  # noqa: E402

app.include_router(backups_router.router)
app.include_router(internal_config.router)
app.include_router(settings_ai_router.router)
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
app.include_router(threads.router)
app.include_router(notes.router)
app.include_router(priority.router)
app.include_router(recommendations.router)
app.include_router(recommendation_feedback.router)
app.include_router(search.router)
app.include_router(discovery.router)
app.include_router(papers.router)
app.include_router(pdf_actions.router)
app.include_router(pdf_files.router)
app.include_router(highlights.router)
app.include_router(rag.router)
app.include_router(pulse_router.router)
app.include_router(zotero_router.router)
app.include_router(system.router)
app.include_router(system_readiness.router)
app.include_router(system_capabilities.router)
app.include_router(jobs.router)
app.include_router(source_config_router.router)
app.include_router(infra_events.router)


# ---------------------------------------------------------------------------
# Health check — probes are service-owned; aggregator + routes live in
# jarvis_common.health.
# ---------------------------------------------------------------------------


async def _probe_qdrant(request: Request) -> str:
    qdrant = getattr(request.app.state, "qdrant_client", None)
    if qdrant is None:
        logger.warning("Health check: Qdrant client missing")
        return "unavailable"

    try:
        exists = await asyncio.wait_for(
            qdrant.collection_exists(COLLECTION_NAME),
            timeout=_QDRANT_HEALTH_TIMEOUT_S,
        )
    except TimeoutError:
        logger.warning("Health check: Qdrant collection probe timed out")
        return "unknown"
    except Exception:
        logger.warning("Health check: Qdrant unavailable", exc_info=True)
        return "unavailable"
    return "ok" if exists else "unavailable"


async def _probe_ollama(request: Request) -> str:
    try:
        ollama_url = get_paper_ingestion_settings().ollama_base_url
        resp = await asyncio.wait_for(
            request.app.state.http_client.get(f"{ollama_url}/api/tags"),
            timeout=5.0,
        )
        return "ok" if resp.status_code == 200 else "unavailable"
    except Exception:
        logger.warning("Health check: Ollama unavailable", exc_info=True)
        return "unavailable"


async def _probe_vector(request: Request) -> str:
    """Vector sidecar probe — the API is optional so failures map to
    ``"unknown"`` (not ``"unavailable"``) and do not drag overall status to
    ``"degraded"``.

    When ``vector_api_url`` is unset (the sidecar is disabled) the probe
    short-circuits to ``"unknown"`` without a network round-trip.
    """
    vector_url = get_paper_ingestion_settings().vector_api_url.strip()
    if not vector_url:
        return "unknown"
    try:
        resp = await asyncio.wait_for(
            request.app.state.http_client.get(f"{vector_url}/health"),
            timeout=3.0,
        )
        return "ok" if resp.status_code == 200 else "unknown"
    except Exception:
        return "unknown"


register_health_routes(
    app,
    service_name="paper_ingestion",
    checks=[
        ("postgres", make_postgres_probe()),
        ("qdrant", _probe_qdrant),
        ("litellm", make_litellm_probe()),
        ("ollama", _probe_ollama),
        ("vector", _probe_vector),
    ],
    limiter=limiter,
)
