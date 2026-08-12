"""System status endpoints: setup wizard readiness + Ollama model info."""

import asyncio
import logging
import re
from typing import Any

import asyncpg
import httpx  # noqa: F401  # tests reach the httpx module via ``routers.system.httpx``
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from jarvis_common import (
    JobCreateResponse,
    current_user_id_or_none,
    require_admin,
    require_admin_or_api_key,
)
from jarvis_common.app_factory import STRUCTURED_DECODING_MODE
from jarvis_common.audit import log_audit
from jarvis_common.config_flags import coerce_bool
from jarvis_common.model_catalog import Role
from pydantic import BaseModel, Field

from paper_ingestion.config import get_paper_ingestion_settings
from paper_ingestion.constants import FAST_MODEL_DEFAULT, SMART_MODEL_DEFAULT
from paper_ingestion.deps import get_db_pool, limiter

# Readiness and capabilities endpoints moved to sibling modules; re-export the
# moved names so existing imports of ``paper_ingestion.routers.system`` resolve.
from paper_ingestion.routers.system_capabilities import (  # noqa: F401
    _GRAMMAR_ENFORCING_MODES,
    SystemCapabilities,
    get_system_capabilities,
)
from paper_ingestion.routers.system_readiness import (  # noqa: F401
    _DEV_FLAG_NAMES,
    _STATUS_RANK,
    ReadinessCheck,
    ReadinessResponse,
    ReadinessStatus,
    get_system_readiness,
)
from paper_ingestion.services.job_enqueue import enqueue_job
from paper_ingestion.services.model_lifecycle import (
    async_get_cached_hardware,
    catalog_entry_for_model,
    normalize_model_tag,
)

# Storage and models-view logic moved to the service layer; re-export the moved
# names so existing imports of ``paper_ingestion.routers.system`` resolve.
from paper_ingestion.services.storage_usage import (  # noqa: F401
    _HF_CACHE_DIR,
    _LOW_DISK_FREE_GB,
    QdrantCollectionUsage,
    StorageResponse,
    StorageSection,
    _dir_size_bytes,
    _disk_pressure,
    _hf_cache_storage_usage,
    _ollama_storage_usage,
    _postgres_storage_usage,
    _qdrant_storage_usage,
)
from paper_ingestion.services.system_models_view import (  # noqa: F401
    _MODEL_ROLES,
    SystemModelsWithDeliveryResponse,
    _cloud_key_presence,
    _compute_model_warnings,
    _get_system_models_data,
    _model_warnings_cache,
    _models_match,
    _ollama_probe_cache,
    _probe_ollama,
    _strip_latest,
)

logger = logging.getLogger(__name__)

_TAG_RE = re.compile(r"^[a-zA-Z0-9_./:@-]{1,200}$")

router = APIRouter(prefix="/api/system", tags=["system"])


class SetupStatus(BaseModel):
    setup_completed: bool
    models_ready: bool
    models_downloading: list[str]
    topics_count: int
    telegram_configured: bool
    telegram_paired: bool
    model_warnings: list[str] = Field(default_factory=list)


@router.get("/setup-status", response_model=SetupStatus, dependencies=[Depends(require_admin)])
@limiter.limit("30/minute")
async def get_setup_status(
    request: Request,
    pool: asyncpg.Pool = Depends(get_db_pool),
) -> SetupStatus:
    """Return a point-in-time snapshot of setup wizard readiness signals."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT key, value FROM user_config WHERE key = ANY($1::text[]) AND user_id IS NULL",
            ["setup.completed"],
        )
        topics_row = await conn.fetchrow("SELECT COUNT(*) AS n FROM topics")
        paired_row = await conn.fetchrow("SELECT COUNT(*) AS n FROM telegram_user_pairings")
        telegram_token_row = await conn.fetchrow(
            "SELECT value, encrypted_value FROM user_config "
            "WHERE key = 'telegram.bot_token' AND user_id IS NULL",
        )

    config: dict[str, Any] = {r["key"]: r["value"] for r in rows}
    setup_completed = coerce_bool(config.get("setup.completed"), default=False)
    telegram_paired = int(paired_row["n"]) > 0 if paired_row else False
    topics_count = int(topics_row["n"]) if topics_row else 0

    # A token saved through the web interface (stored in user_config) takes
    # priority over the environment value at bot startup — see
    # telegram_bot/config.py's DB-first token resolution. Either source having
    # a value makes the bot "configured" once it next starts.
    telegram_token_stored = telegram_token_row is not None and (
        telegram_token_row["value"] is not None or telegram_token_row["encrypted_value"] is not None
    )
    telegram_configured = (
        bool(get_paper_ingestion_settings().telegram_bot_token) or telegram_token_stored
    )

    models_ready, models_downloading = await _probe_ollama()

    model_warnings = await _compute_model_warnings()

    return SetupStatus(
        setup_completed=setup_completed,
        models_ready=models_ready,
        models_downloading=models_downloading,
        topics_count=topics_count,
        telegram_configured=telegram_configured,
        telegram_paired=telegram_paired,
        model_warnings=model_warnings,
    )


# ---------------------------------------------------------------------------
# GET /api/system/models
# ---------------------------------------------------------------------------


@router.get(
    "/models",
    response_model=SystemModelsWithDeliveryResponse,
    dependencies=[Depends(require_admin_or_api_key)],
)
@limiter.limit("30/minute")
async def get_system_models(request: Request) -> SystemModelsWithDeliveryResponse:
    """Return installed Ollama models + hardware info + current assignments."""
    return await _get_system_models_data(request)


@router.get("/hardware", dependencies=[Depends(require_admin_or_api_key)])
@limiter.limit("10/minute")
async def get_system_hardware(request: Request) -> dict[str, Any]:
    """Return local accelerator information for model selection."""
    return (await async_get_cached_hardware(request.app.state)).to_dict()


@router.get("/models/recommendations", dependencies=[Depends(require_admin_or_api_key)])
@limiter.limit("30/minute")
async def get_model_recommendations(
    request: Request,
    role: Role = "smart",
) -> dict[str, Any]:
    """Return catalog-backed model recommendations for one role.

    Reuses the models response's own recommendations rather than recomputing
    them, so this endpoint cannot disagree with the picker about which models
    exist.
    """
    models = await _get_system_models_data(request)
    return {"role": role, "recommendations": models.recommendations.get(role, [])}


@router.post(
    "/models/{tag:path}/pull",
    dependencies=[Depends(require_admin_or_api_key)],
)
@limiter.limit("3/minute")
async def pull_system_model(
    tag: str,
    request: Request,
    user_id: int | None = Depends(current_user_id_or_none),
) -> JobCreateResponse:
    """Enqueue an Ollama model pull job and return immediately."""
    if not _TAG_RE.match(tag):
        raise HTTPException(status_code=422, detail="Invalid model tag format")
    entry = catalog_entry_for_model(tag)
    if entry is None or entry.provider != "ollama":
        raise HTTPException(status_code=404, detail="Unknown local catalog model")
    from jarvis_common.task_registry import KIND_TO_TASK

    task = KIND_TO_TASK.get("model.pull")
    if task is None:
        raise HTTPException(status_code=500, detail="model.pull task is not registered")
    enqueued = await enqueue_job(
        task,
        user_id=user_id,
        ollama_tag=entry.ollama_tag or entry.id,
        ollama_url=get_paper_ingestion_settings().ollama_base_url,
    )
    caller_id = getattr(request.state, "user_id", None)
    await log_audit(
        request.app.state.db_pool,
        action="system.model.pull",
        resource=f"models/{tag}",
        user_id=str(caller_id) if caller_id is not None else None,
    )
    return enqueued


@router.delete(
    "/models/{tag:path}",
    status_code=204,
    response_class=Response,
    dependencies=[Depends(require_admin_or_api_key)],
)
@limiter.limit("3/minute")
async def delete_system_model(tag: str, request: Request) -> Response:
    """Delete an inactive Ollama model tag."""
    if not _TAG_RE.match(tag):
        raise HTTPException(status_code=422, detail="Invalid model tag format")
    entry = catalog_entry_for_model(tag)
    if entry is None or entry.provider != "ollama":
        raise HTTPException(status_code=404, detail="Unknown local catalog model")

    models = await _get_system_models_data(request)
    if models.issues.get("current"):
        raise HTTPException(
            status_code=503,
            detail=(
                "The stored model routes could not be read just now, so this model "
                "was left in place. Please try again in a moment."
            ),
        )

    normalized_tag = normalize_model_tag(tag)
    active = {normalize_model_tag(str(value)) for value in models.current.values()}
    if normalized_tag in active:
        raise HTTPException(status_code=409, detail="Cannot delete an active model assignment")

    http = request.app.state.http_client
    ollama_url = get_paper_ingestion_settings().ollama_base_url
    delete_name = entry.ollama_tag or entry.id
    try:
        resp = await http.request(
            "DELETE",
            f"{ollama_url}/api/delete",
            json={"name": delete_name},
            timeout=120.0,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Could not reach Ollama delete API") from exc
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail="Ollama model delete failed")
    caller_id = getattr(request.state, "user_id", None)
    await log_audit(
        request.app.state.db_pool,
        action="system.model.delete",
        resource=f"models/{tag}",
        user_id=str(caller_id) if caller_id is not None else None,
    )
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# GET /api/system/storage
# ---------------------------------------------------------------------------


@router.get(
    "/storage",
    response_model=StorageResponse,
    dependencies=[Depends(require_admin_or_api_key)],
)
@limiter.limit("30/minute")
async def get_system_storage(
    request: Request,
    pool: asyncpg.Pool = Depends(get_db_pool),
) -> StorageResponse:
    """Disk-usage snapshot across Ollama models, Postgres, Qdrant, and the HF cache."""
    qdrant_section, qdrant_collections = await _qdrant_storage_usage(
        getattr(request.app.state, "qdrant_client", None)
    )
    return StorageResponse(
        ollama_models=await _ollama_storage_usage(
            request.app.state.http_client,
            get_paper_ingestion_settings().ollama_base_url,
        ),
        postgres=await _postgres_storage_usage(pool),
        qdrant=qdrant_section,
        qdrant_collections=qdrant_collections,
        hf_cache=await asyncio.to_thread(_hf_cache_storage_usage),
        pressure=await asyncio.to_thread(_disk_pressure),
    )


# ---------------------------------------------------------------------------
# GET /api/system/effective-config
# ---------------------------------------------------------------------------

# Concrete code-default model per role. smart/fast mirror the static fallbacks
# in main.py (_LITELLM_ROLE_FALLBACKS) — reconstructed here because main.py is
# the FastAPI entrypoint and importing it has module-load side effects. embed
# and pulse_stage2 default to LiteLLM aliases, sourced from PaperIngestionSettings.
_ROLE_CODE_DEFAULTS: dict[str, str] = {
    "smart": SMART_MODEL_DEFAULT,
    "fast": FAST_MODEL_DEFAULT,
}

# Static deployment invariant: litellm/config.yaml sets `drop_params: true`
# (LiteLLM's own config, not parsed by the app). Reported as a constant rather
# than a runtime yaml read — the value is version-controlled and never varies
# per request.
_LITELLM_DROP_PARAMS = True


class ResolvedRole(BaseModel):
    """Resolved model selection for one LLM role: code default vs effective value."""

    code_default: str
    effective: str
    transport_prefix: str


class EffectiveConfig(BaseModel):
    """Static resolved-vs-default config snapshot: the silent-override control.

    Diffing ``effective`` against ``code_default`` per role is what surfaces an
    override like ``PULSE_STAGE2_MODEL=fast`` shadowing the ``smart`` default.
    """

    roles: dict[str, ResolvedRole]
    instructor_mode: str
    structured_output_enforced: bool
    drop_params: bool
    think_disabled: dict[str, bool]


@router.get(
    "/effective-config",
    response_model=EffectiveConfig,
    dependencies=[Depends(require_admin_or_api_key)],
)
@limiter.limit("30/minute")
async def get_effective_config(request: Request) -> EffectiveConfig:
    """Return the resolved model roles and structured-output enforcement state.

    No live LLM/HTTP call — one cheap DB read for the smart/fast/embed overrides
    plus static settings/constants. The effective value is the committed DB
    override when present, else the settings/env default; pulse_stage2 resolves
    from ``PaperIngestionSettings`` (env), mirroring ``pulse/scoring.py``.
    """
    from paper_ingestion.services.litellm_config import (  # noqa: PLC0415
        ALIAS_BOOTSTRAP_PARAMS,
    )

    settings = get_paper_ingestion_settings()

    overrides: dict[str, str] = {}
    try:
        async with request.app.state.db_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT key, value FROM user_config "
                "WHERE key = ANY($1::text[]) AND user_id IS NULL",
                ["llm.smart_model", "llm.fast_model", "llm.embed_model"],
            )
        for r in rows:
            val = r["value"]
            if isinstance(val, str):
                val = val.strip('"')
            overrides[r["key"].replace("llm.", "")] = val
    except Exception:
        logger.warning("Could not load resolved model overrides", exc_info=True)

    def _transport_prefix(alias: str) -> str:
        return "ollama/" if alias == "embed" else "ollama_chat/"

    embed_default = type(settings).model_fields["embedding_model"].default
    pulse_default = type(settings).model_fields["pulse_stage2_model"].default
    roles = {
        "smart": ResolvedRole(
            code_default=_ROLE_CODE_DEFAULTS["smart"],
            effective=overrides.get("smart_model") or _ROLE_CODE_DEFAULTS["smart"],
            transport_prefix=_transport_prefix("smart"),
        ),
        "fast": ResolvedRole(
            code_default=_ROLE_CODE_DEFAULTS["fast"],
            effective=overrides.get("fast_model") or _ROLE_CODE_DEFAULTS["fast"],
            transport_prefix=_transport_prefix("fast"),
        ),
        "embed": ResolvedRole(
            code_default=embed_default,
            effective=overrides.get("embed_model") or embed_default,
            transport_prefix=_transport_prefix("embed"),
        ),
        "pulse_stage2": ResolvedRole(
            code_default=pulse_default,
            effective=settings.pulse_stage2_model or pulse_default,
            transport_prefix=_transport_prefix("pulse_stage2"),
        ),
    }

    think_disabled = {
        alias: params.get("think") is False
        for alias, params in ALIAS_BOOTSTRAP_PARAMS.items()
        if alias in ("smart", "fast")
    }

    return EffectiveConfig(
        roles=roles,
        instructor_mode=STRUCTURED_DECODING_MODE,
        structured_output_enforced=STRUCTURED_DECODING_MODE in _GRAMMAR_ENFORCING_MODES,
        drop_params=_LITELLM_DROP_PARAMS,
        think_disabled=think_disabled,
    )
