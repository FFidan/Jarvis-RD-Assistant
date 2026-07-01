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
import contextlib
import dataclasses
import logging
import os
import socket
from collections.abc import Mapping, Sequence
from typing import Any

import asyncpg
import httpx
from fastapi import Depends, FastAPI, Request
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
from jarvis_common.auth import validate_runtime_config
from jarvis_common.cached_transport import CachingTransport
from jarvis_common.db_helpers import _ALIAS_MODELS
from jarvis_common.health import make_litellm_probe, make_postgres_probe
from jarvis_common.settings import get_core_settings, get_secrets_settings
from jarvis_common.verify import QuoteVerifier
from jarvis_common.warmup import make_warmup_hook, warm_chat_model, warm_embedding_model
from qdrant_client import AsyncQdrantClient

# Trigger source registration via imports
import paper_ingestion.sources  # noqa: F401
from paper_ingestion.config import get_paper_ingestion_settings
from paper_ingestion.constants import FAST_MODEL_DEFAULT, SMART_MODEL_DEFAULT
from paper_ingestion.deps import limiter
from paper_ingestion.ingestion.embedder import Embedder
from paper_ingestion.ingestion.embedding_config import (
    COLLECTION_NAME,
    EMBEDDING_MODEL,
    EMBEDDING_MODEL_NAME,
)
from paper_ingestion.integrations.zotero_client import validate_bbt_base_url
from paper_ingestion.migrations_runner import run_migrations
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
from paper_ingestion.services.telegram_bootstrap import refresh_telegram_bot_username
from paper_ingestion.sources.registry import get_source_class

# Install uvloop early so the event-loop policy is set before
# any asyncio.get_event_loop() calls.  Guarded against pytest runs because
# uvloop.install() mutates the global policy and breaks pytest-asyncio
# per-test loop isolation (tests pass when isolated but fail as a suite).
if not os.environ.get("PYTEST_CURRENT_TEST"):
    try:
        import uvloop  # noqa: PLC0415

        uvloop.install()
    except ImportError:
        pass

configure_logging("paper_ingestion", log_level=get_core_settings().log_level)
maybe_init_sentry("paper_ingestion")

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
    """Bridge for ``make_init_langfuse_hook`` — populates ``paper_ingestion._state.svc``."""
    from paper_ingestion._state import set_services  # noqa: PLC0415

    set_services(openai_client=openai_client)


async def _validate_bbt_url_hook(app: FastAPI) -> None:
    """Validate ``BBT_BASE_URL`` to block file:// + unknown private IPs.

    Must run before any Zotero integration touches the URL — the SSRF surface
    area is the BBT translator endpoint.
    """
    validate_bbt_base_url()


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


async def _migrate_plaintext_secrets_hook(app: FastAPI) -> None:
    """Eagerly re-encrypt any legacy plaintext secret rows in user_config.

    Runs after migrations so the schema is at the latest version. Best-effort:
    failures are logged inside the helper so this hook never blocks startup.
    """
    from paper_ingestion.services.config_db import (  # noqa: PLC0415
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


async def _refresh_telegram_username(app: FastAPI) -> None:
    """Refresh cached Telegram bot username for setup-wizard pairing links."""
    await refresh_telegram_bot_username(app.state.db_pool, app.state.http_client)


# ---------------------------------------------------------------------------
# LiteLLM model reconciler — replaces the old boot-time "_rehydrate" pass.
#
# litellm/config.yaml deliberately carries NO smart/fast/smart-fallback aliases
# (a YAML alias can never be deleted at runtime and would STACK with its DB
# replacement). The reconciler therefore guarantees those deployments exist in
# LiteLLM's admin DB: a persistent background loop runs one pass ~every 30 s
# (matching LiteLLM's own DB-reconciler cadence), marking the affected roles in
# ``llm.delivery_pending`` while undelivered so the UI shows an honest
# "pending — applying automatically" pill instead of a phantom "applied".
# A LiteLLM stuck DB-less (prisma migrate failed) stays visibly pending while
# the loop keeps retrying — a loud, honest degradation instead of a silently
# LLM-dead deployment.
# ---------------------------------------------------------------------------

_LITELLM_RECONCILE_INTERVAL_SECONDS = 30.0

# Desired-model resolution, one-way precedence: user_config (Settings choice)
# wins; else the setup-chosen .env value when the env var is present in this
# process; else the static always-pulled default (OLLAMA_MODELS coherence).
_LITELLM_ROLE_FALLBACKS: dict[str, tuple[str, str]] = {
    "llm.smart_model": ("JARVIS_SMART_MODEL", SMART_MODEL_DEFAULT),
    "llm.fast_model": ("JARVIS_FAST_MODEL", FAST_MODEL_DEFAULT),
}

# Transition-aware failure logging for the persistent reconciler. A degraded
# LiteLLM would otherwise emit a full WARNING+traceback per role every 30 s
# (~5,760 tracebacks/day). Policy: full traceback on the FIRST consecutive
# failure per delivery target, then one terse WARNING every
# _RECONCILE_TERSE_EVERY_N passes (~10 min at the 30 s cadence) while the
# failure persists; the streak resets on success so the next distinct outage
# logs a fresh traceback. The recovery transition stays the loop's
# "all deployments reconciled" INFO.
_RECONCILE_TERSE_EVERY_N = 20
_RECONCILE_FAILURE_STREAKS: dict[str, int] = {}

# One-shot anomaly logs: a stored bare-alias placeholder and a legacy
# embed-model mismatch repeat identically on every pass — log each distinct
# value once per process lifetime instead of every 30 s.
_ALIAS_PLACEHOLDER_LOGGED: set[tuple[str, str]] = set()
_EMBED_MISMATCH_WARNED: set[str] = set()
# Keep below jarvis_common.health._PROBE_TIMEOUT_S so Qdrant metadata slowness
# is classified by _probe_qdrant as "unknown", not by the outer sweep as
# degraded "timeout".
_QDRANT_HEALTH_TIMEOUT_S = 4.0


def _log_reconcile_failure(target: str) -> None:
    """Streak-aware delivery-failure logging (call from an ``except`` block)."""
    streak = _RECONCILE_FAILURE_STREAKS.get(target, 0)
    if streak == 0:
        logger.warning(
            "LiteLLM reconcile: could not deliver %s; will retry",
            target,
            exc_info=True,
        )
    elif streak % _RECONCILE_TERSE_EVERY_N == 0:
        logger.warning(
            "LiteLLM reconcile: still cannot deliver %s (%d consecutive failures); will retry",
            target,
            streak + 1,
        )
    _RECONCILE_FAILURE_STREAKS[target] = streak + 1


async def _mark_role_pending(pool: Any, role: str, pending: bool) -> None:
    """Best-effort llm.delivery_pending bookkeeping — never raises."""
    from paper_ingestion.services.config_write import (  # noqa: PLC0415
        _update_delivery_pending_roles,
    )

    try:
        await _update_delivery_pending_roles(pool, roles={role}, pending=pending)
    except Exception:
        logger.warning(
            "Could not update llm.delivery_pending for role %s during reconcile",
            role,
            exc_info=True,
        )


async def _desired_model_for_role(pool: Any, config_key: str) -> str:
    """Resolve the model the *config_key* role should route (see precedence above)."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT value FROM user_config WHERE key = $1 AND user_id IS NULL", config_key
        )
    if row is not None:
        model_id = str(row["value"])
        if model_id in _ALIAS_MODELS:
            # Defense-in-depth: a stray re-seed could store the bare alias
            # ("smart"); forwarding it would create ollama/smart → 404.
            # Logged once per distinct value — this resolves every pass.
            if (config_key, model_id) not in _ALIAS_PLACEHOLDER_LOGGED:
                _ALIAS_PLACEHOLDER_LOGGED.add((config_key, model_id))
                logger.info(
                    "Ignoring stored value for %s: %r is an alias placeholder, not a model",
                    config_key,
                    model_id,
                )
        elif model_id:
            return model_id
    env_name, static_default = _LITELLM_ROLE_FALLBACKS[config_key]
    return os.environ.get(env_name, "").strip() or static_default


async def _reconcile_litellm_models_once(pool: Any) -> bool:
    """One reconcile pass. Returns True when every delivery is verified.

    Per-role failures are caught, logged, and marked pending — a pass never
    raises for delivery errors, so the surrounding loop (and the lifespan)
    survives LiteLLM still warming up or running DB-less.
    """
    from paper_ingestion.services.litellm_config import (  # noqa: PLC0415
        ROLE_TO_ALIAS,
        _config_lock,
        ensure_smart_fallback,
        update_litellm_model,
    )

    machine_id = socket.gethostname()
    all_ok = True
    desired_by_key: dict[str, str] = {}
    for config_key in _LITELLM_ROLE_FALLBACKS:
        role = ROLE_TO_ALIAS[config_key]
        try:
            # _config_lock serializes against Settings PUT deliveries: an
            # interleaved /model/new + /model/delete pair could delete a
            # deployment a concurrent request just created. The row read sits
            # INSIDE the lock so a racing PUT and this pass converge on the
            # same committed value regardless of order.
            async with _config_lock:
                desired = await _desired_model_for_role(pool, config_key)
                desired_by_key[config_key] = desired
                await update_litellm_model(config_key, desired, db_pool=pool, machine_id=machine_id)
        except Exception:
            _log_reconcile_failure(config_key)
            await _mark_role_pending(pool, role, True)
            all_ok = False
            continue
        _RECONCILE_FAILURE_STREAKS.pop(config_key, None)
        # Delivered (True) or already routing the committed model (False) —
        # either way LiteLLM routes the desired model: clear any stale marker.
        await _mark_role_pending(pool, role, False)

    # smart-fallback: the real deployment group behind router_settings'
    # smart → ["smart-fallback"] mapping (fast-tier model, timeout 120). Not a
    # settings role, so it has no pending marker — just retry until it exists.
    if all_ok:
        try:
            async with _config_lock:
                await ensure_smart_fallback(
                    desired_by_key["llm.fast_model"], db_pool=pool, machine_id=machine_id
                )
        except Exception:
            _log_reconcile_failure("smart-fallback")
            all_ok = False
        else:
            _RECONCILE_FAILURE_STREAKS.pop("smart-fallback", None)

    # embed is dimension-locked and YAML-seeded — never delivered here. A
    # stored llm.embed_model that differs from the static default needs a
    # deliberate YAML edit + re-embed, so only warn (no pending: no automatic
    # delivery is coming for it).
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT value FROM user_config WHERE key = 'llm.embed_model' AND user_id IS NULL"
        )
    if row is not None:
        embed_model = str(row["value"])
        if (
            embed_model not in _ALIAS_MODELS
            and embed_model != EMBEDDING_MODEL_NAME
            # Legacy rows trip this on EVERY pass — warn once per distinct value.
            and embed_model not in _EMBED_MISMATCH_WARNED
        ):
            _EMBED_MISMATCH_WARNED.add(embed_model)
            logger.warning(
                "llm.embed_model is %r but the embed alias is YAML-seeded with %r; "
                "switching embedders requires editing litellm/config.yaml and re-embedding",
                embed_model,
                EMBEDDING_MODEL_NAME,
            )

    return all_ok


async def _litellm_model_reconciler_loop(pool: Any) -> None:
    """Persistent reconcile loop: one pass ~every 30 s for the process lifetime.

    WHY persistent (not stop-on-first-success): a LiteLLM that later restarts
    against an unreachable admin DB (prisma migrate WARN-and-continue) comes
    back DB-less with ONLY the YAML models — i.e. with NO smart/fast
    deployments at all, because the YAML is de-seeded — and a stopped loop
    would leave the deployment LLM-dead until an operator restarts this
    service. The ~30 s cadence matches LiteLLM's own DB reconciler; a
    steady-state pass costs three cheap GETs that no-op on comparison. This
    loop is also the re-convergence path for a delivered-but-uncommitted
    settings write (PUT failed after /model/new succeeded): the next pass
    routes LiteLLM back to the still-stored row.
    """
    reconciled_logged = False
    while True:
        try:
            if await _reconcile_litellm_models_once(pool):
                if not reconciled_logged:
                    logger.info("LiteLLM model reconciler: all deployments reconciled")
                    reconciled_logged = True
            else:
                reconciled_logged = False
        except asyncio.CancelledError:
            raise
        except Exception:
            # Belt-and-braces: per-role errors are handled inside the pass;
            # this catches infrastructure failures (e.g. DB pool teardown).
            logger.warning(
                "LiteLLM model reconciler pass failed unexpectedly; retrying",
                exc_info=True,
            )
            reconciled_logged = False
        await asyncio.sleep(_LITELLM_RECONCILE_INTERVAL_SECONDS)


async def _start_litellm_reconciler(app: FastAPI) -> None:
    """Start the persistent LiteLLM model reconciler as a background task."""
    app.state.litellm_reconciler_task = asyncio.create_task(
        _litellm_model_reconciler_loop(app.state.db_pool),
        name="litellm_model_reconciler",
    )


async def _shutdown_litellm_reconciler(app: FastAPI) -> None:
    """Cancel the reconciler task and await its termination (clean teardown)."""
    task = getattr(app.state, "litellm_reconciler_task", None)
    if task is None or task.done():
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


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
    from paper_ingestion.services.litellm_config import ROLE_TO_ALIAS  # noqa: PLC0415

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


# B.4 Step 4 — start the procrastinate worker polling paper_ingestion + builtin
# Hook body is shared via jarvis_common.app_factory.
_start_procrastinate_worker = make_procrastinate_worker_hook(
    _register_tasks, queues=["paper_ingestion", "builtin"]
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
        "transport": CachingTransport(httpx.AsyncHTTPTransport()),
    },
    custom_init_tasks=[
        make_init_langfuse_hook(_set_openai_client),
        _validate_bbt_url_hook,
        _run_migrations_hook,
        _validate_runtime_config_hook,
        _run_hw_probe_hook,
        _migrate_plaintext_secrets_hook,
        _init_qdrant_and_pdf_pipeline,
        _init_source_singletons,
        _refresh_telegram_username,
        _autoconfigure_models_hook,
        _start_litellm_reconciler,
        _start_scheduler_hook,
        _start_procrastinate_worker,
        make_warmup_hook(
            lambda app: [
                lambda: warm_embedding_model(app.state.http_client, EMBEDDING_MODEL),
                lambda: warm_chat_model(app.state.http_client),
            ]
        ),
    ],
    # Index-aligned with custom_init_tasks; None = no teardown counterpart.
    custom_teardown_tasks=[
        None,  # init_langfuse_hook (Langfuse SDK auto-flushes on process exit)
        None,  # _validate_bbt_url_hook
        None,  # _run_migrations_hook
        None,  # _validate_runtime_config_hook
        None,  # _run_hw_probe_hook
        None,  # _migrate_plaintext_secrets_hook
        _shutdown_qdrant,  # _init_qdrant_and_pdf_pipeline
        None,  # _init_source_singletons
        None,  # _refresh_telegram_username
        None,  # _autoconfigure_models_hook
        _shutdown_litellm_reconciler,  # _start_litellm_reconciler
        _shutdown_scheduler,  # _start_scheduler_hook
        _shutdown_procrastinate_worker,  # _start_procrastinate_worker
        None,  # make_warmup_hook (fire-and-forget; cancelled at process exit)
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

# ---------------------------------------------------------------------------
# Router registration
# ---------------------------------------------------------------------------

from paper_ingestion.routers import account as account_router  # noqa: E402
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
    highlights,
    infra_events,
    jobs,
    knowledge_graph,
    logs,
    my_day,
    notes,
    papers,
    pdf,
    pdfs,
    priority,
    rag,
    recommendation_feedback,
    recommendations,
    search,
    settings,
    snapshots,
    system,
    telegram,
    threads,
    topics,
)
from paper_ingestion.routers import audit_admin as audit_admin_router  # noqa: E402
from paper_ingestion.routers import auth as auth_router  # noqa: E402
from paper_ingestion.routers import backups as backups_router  # noqa: E402
from paper_ingestion.routers import pulse as pulse_router  # noqa: E402
from paper_ingestion.routers import settings_ai as settings_ai_router  # noqa: E402
from paper_ingestion.routers import setup as setup_router  # noqa: E402
from paper_ingestion.routers import source_config as source_config_router  # noqa: E402
from paper_ingestion.routers import zotero as zotero_router  # noqa: E402

app.include_router(auth_router.router)
# Admin router uses session-only auth (no X-API-Key required for browser
# sessions). Exempt from the global verify_api_key dep via dependencies=[].
app.include_router(admin_router.router, dependencies=[])
# Backup panel uses session-only admin auth (no X-API-Key); exempt from the
# global verify_api_key dep like admin.py.
app.include_router(backups_router.router, dependencies=[])
# AI backend configuration — session-only admin auth, no X-API-Key required.
app.include_router(settings_ai_router.router, dependencies=[])
# Setup router is the first-run bootstrap. Endpoints are wide open until
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
app.include_router(threads.router)
app.include_router(notes.router)
app.include_router(priority.router)
app.include_router(recommendations.router)
app.include_router(recommendation_feedback.router)
app.include_router(search.router)
app.include_router(discovery.router)
app.include_router(papers.router)
app.include_router(pdf.router)
app.include_router(pdfs.router)
app.include_router(highlights.router)
app.include_router(rag.router)
app.include_router(pulse_router.router)
app.include_router(zotero_router.router)
app.include_router(telegram.router)
app.include_router(system.router)
app.include_router(jobs.router)
app.include_router(logs.router)
app.include_router(audit_admin_router.router)
app.include_router(source_config_router.router)
app.include_router(infra_events.router)
# Account — self-service current-user profile (GET/PATCH /api/account
# + verified email change). Distinct from admin user-mgmt (/api/admin/*).
app.include_router(account_router.router)


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


# ---------------------------------------------------------------------------
# Back-compat shims (imported by tests and internal lazy imports)
# ---------------------------------------------------------------------------

# run_migrations is imported directly from paper_ingestion.migrations_runner;
# re-export here so existing `from paper_ingestion.main import run_migrations` still works.
# (already imported at top of file)

# get_system_models is now served by paper_ingestion.routers.system;
# re-export the router function for test_brief_and_models.py back-compat.
from paper_ingestion.routers.system import get_system_models  # noqa: E402,F401
