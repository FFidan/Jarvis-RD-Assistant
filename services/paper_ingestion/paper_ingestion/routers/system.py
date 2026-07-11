"""System status endpoints: setup wizard readiness + Ollama model info."""

import asyncio
import logging
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

import asyncpg
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from jarvis_common import (
    JobCreateResponse,
    current_user_id,
    require_admin,
    require_admin_or_api_key,
)
from jarvis_common.app_factory import STRUCTURED_DECODING_MODE
from jarvis_common.audit import log_audit
from jarvis_common.hardware_fit import recommend_models
from jarvis_common.model_catalog import Role
from jarvis_common.serialization import _coerce_bool
from pydantic import BaseModel, Field

from paper_ingestion.config import get_paper_ingestion_settings
from paper_ingestion.constants import FAST_MODEL_DEFAULT, SMART_MODEL_DEFAULT
from paper_ingestion.deps import get_db_pool, limiter
from paper_ingestion.ingestion.embedder import (
    EMBEDDING_DIMENSION,
    EMBEDDING_MODEL_NAME,
    validate_embedding_configuration,
)
from paper_ingestion.models import SystemModelsResponse

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
from paper_ingestion.services.model_lifecycle import (
    HardwareInfo,
    async_get_cached_hardware,
    build_model_statuses,
    catalog_entry_for_model,
    normalize_model_tag,
    recommendations_for_role,
)
from paper_ingestion.services.model_prefixes import strip_ollama_prefix

logger = logging.getLogger(__name__)

_TAG_RE = re.compile(r"^[a-zA-Z0-9_./:@-]{1,200}$")

router = APIRouter(prefix="/api/system", tags=["system"])

# Derive the embedder family prefix from EMBEDDING_MODEL_NAME at import time
# so setup-status always tracks whatever is actually configured.
# e.g. "qwen3-embedding:4b" → "qwen3-embedding"
_EMBEDDER_BASE: str = EMBEDDING_MODEL_NAME.split(":")[0]

_OLLAMA_PROBE_TTL = 10  # seconds


class _OllamaProbeCache:
    """TTL cache for Ollama /api/tags probe results.

    Replaces module-level ``global`` mutation with a contained state holder.
    """

    def __init__(self) -> None:
        self._ts: float = 0.0
        self._result: tuple[bool, list[str]] = (False, [])

    def get_cached(self, now: float) -> tuple[bool, list[str]] | None:
        if self._ts > 0 and now - self._ts < _OLLAMA_PROBE_TTL:
            return self._result
        return None

    def set(self, now: float, result: tuple[bool, list[str]]) -> None:
        self._ts = now
        self._result = result


_ollama_probe_cache = _OllamaProbeCache()

_MODEL_WARNINGS_TTL = 30  # seconds


class _ModelWarningsCache:
    """TTL cache for the setup-status model-warnings probe.

    ``_compute_model_warnings`` fires uncached litellm ``/v1/model/info`` +
    ollama ``/api/tags`` calls on every setup-status poll; this caches the
    computed warning list (~30 s) mirroring ``_OllamaProbeCache`` above.
    """

    def __init__(self) -> None:
        self._ts: float = 0.0
        self._result: list[str] = []

    def get_cached(self, now: float) -> list[str] | None:
        if self._ts > 0 and now - self._ts < _MODEL_WARNINGS_TTL:
            return self._result
        return None

    def set(self, now: float, result: list[str]) -> None:
        self._ts = now
        self._result = result


_model_warnings_cache = _ModelWarningsCache()


class SetupStatus(BaseModel):
    setup_completed: bool
    models_ready: bool
    models_downloading: list[str]
    topics_count: int
    telegram_configured: bool
    telegram_paired: bool
    model_warnings: list[str] = Field(default_factory=list)


def _record_get(row: Any, key: str) -> Any:
    if hasattr(row, "get"):
        return row.get(key)
    try:
        return row[key]
    except Exception:
        return None


def _strip_latest(model: str) -> str:
    """Ollama's implicit default tag (``qwen3:8b`` vs ``qwen3:8b:latest``) must
    never cause a false divergence — applied to BOTH sides of every
    intent ↔ routing compare and to the installed-model set.
    """
    if model.endswith(":latest"):
        return model[:-7]
    return model


def _models_match(installed_names: list[str]) -> bool:
    """Return True iff a qwen3 chat model AND the embedder are both installed.

    Ready condition:
      - At least one model whose name starts with ``"qwen3:"`` (this prefix
        matches qwen3:4b / qwen3:8b / qwen3:14b and explicitly EXCLUDES the
        embedder whose name begins with ``"qwen3-embedding:"``).
      - At least one model whose name starts with ``_EMBEDDER_BASE`` (derived
        from EMBEDDING_MODEL_NAME — no hardcoded tag).
    """
    if not installed_names:
        return False
    has_chat = any(name.startswith("qwen3:") for name in installed_names)
    has_embed = any(name.startswith(_EMBEDDER_BASE) for name in installed_names)
    return has_chat and has_embed


async def _probe_ollama() -> tuple[bool, list[str]]:
    """Probe ``{OLLAMA_BASE_URL}/api/tags``; return (models_ready, downloading).

    Results are cached for 10 seconds to avoid hammering Ollama on every
    setup-status request. Any failure (network, timeout, non-200) yields
    ``(False, [])``. The caller must never crash on this.
    """
    now = time.monotonic()
    cached = _ollama_probe_cache.get_cached(now)
    if cached is not None:
        return cached

    ollama_url = get_paper_ingestion_settings().ollama_base_url
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{ollama_url}/api/tags")
        if resp.status_code != 200:
            # Log so a reachable-but-erroring Ollama (e.g. 503 during startup)
            # is greppable, not silently indistinguishable from "unreachable".
            logger.warning("setup-status: Ollama /api/tags returned %s", resp.status_code)
            result: tuple[bool, list[str]] = (False, [])
            _ollama_probe_cache.set(now, result)
            return result
        data = resp.json()
        installed = [m.get("name", "") for m in data.get("models", [])]
        if _models_match(installed):
            result = (True, [])
        else:
            # Ollama is reachable but provisioning is incomplete.  Build a
            # short missing-pieces list so SystemCheck can show "still pulling"
            # rather than the generic "not ready" message.
            # Chat family uses a stable "qwen3:" prefix; the embedder name is
            # config-derived (_EMBEDDER_BASE from EMBEDDING_MODEL_NAME) — hence
            # the asymmetric checks.
            missing: list[str] = []
            if not any(n.startswith("qwen3:") for n in installed):
                missing.append("qwen3 chat model")
            if not any(n.startswith(_EMBEDDER_BASE) for n in installed):
                missing.append(EMBEDDING_MODEL_NAME)
            result = (False, missing)
    except Exception:
        logger.warning("setup-status: Ollama probe failed", exc_info=True)
        result = (False, [])
    _ollama_probe_cache.set(now, result)
    return result


async def _fetch_litellm_deployments_safe() -> list[Any] | None:
    """Fetch LiteLLM deployments for the model-warnings probe; None on failure."""
    try:
        from paper_ingestion.services.litellm_config import (  # noqa: PLC0415
            get_litellm_deployments,
        )

        return await get_litellm_deployments()
    except Exception:
        logger.debug("model_warnings: LiteLLM probe failed — skipping", exc_info=True)
        return None


def _build_role_routing_map(deployments: list[Any]) -> dict[str, str]:
    """Build role → routed-model map (ollama/ prefix stripped + :latest stripped)."""
    role_to_routed: dict[str, str] = {}
    for dep in deployments:
        alias = dep.model_name
        if alias not in _MODEL_ROLES:
            continue
        params = dep.litellm_params
        routed_full = str(params.get("model", ""))
        if not routed_full:
            continue
        routed_full = strip_ollama_prefix(routed_full)
        role_to_routed[alias] = _strip_latest(routed_full)
    return role_to_routed


async def _fetch_installed_ollama_models(ollama_url: str) -> set[str] | None:
    """Fetch installed Ollama model names (:latest-stripped); None on failure."""
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{ollama_url}/api/tags")
        if resp.status_code != 200:
            # Reachable-but-erroring Ollama (e.g. 503 during startup): log the
            # status so it is greppable, then degrade to no warnings.
            logger.debug(
                "model_warnings: Ollama /api/tags returned %s — skipping", resp.status_code
            )
            return None
        data = resp.json()
        return {_strip_latest(m.get("name", "")) for m in data.get("models", [])}
    except Exception:
        logger.debug("model_warnings: Ollama probe failed — skipping", exc_info=True)
        return None


async def _compute_model_warnings() -> list[str]:
    """Return per-role routing warnings for GET /api/system/setup-status.

    Compares the roles LiteLLM is *currently* routing against the set of models
    Ollama has pulled, using the same ``:latest``-tolerant normalization as the
    routing-truth consistency check.  Always returns ``[]`` when LiteLLM or
    Ollama is unreachable — the endpoint must never fail because of this probe.

    Result is cached ~30 s (``_model_warnings_cache``) so setup-status polling
    does not re-hit litellm + ollama on every request.
    """
    now = time.monotonic()
    cached = _model_warnings_cache.get_cached(now)
    if cached is not None:
        return cached

    deployments = await _fetch_litellm_deployments_safe()
    if deployments is None:
        _model_warnings_cache.set(now, [])
        return []

    role_to_routed = _build_role_routing_map(deployments)
    if not role_to_routed:
        _model_warnings_cache.set(now, [])
        return []

    ollama_url = get_paper_ingestion_settings().ollama_base_url
    installed = await _fetch_installed_ollama_models(ollama_url)
    if installed is None:
        _model_warnings_cache.set(now, [])
        return []

    # Emit a warning for each Ollama role that routes a model not yet pulled.
    # Cloud models (containing "/") are skipped — pulled check is Ollama-specific.
    warnings: list[str] = []
    for role in ("smart", "fast"):
        routed = role_to_routed.get(role)
        if routed is None or "/" in routed:
            continue
        if routed not in installed:
            warnings.append(f"{role} routes to {routed} which is not pulled")

    _model_warnings_cache.set(now, warnings)
    return warnings


async def _cloud_key_presence(pool: asyncpg.Pool) -> dict[str, bool]:
    """Return whether each cloud provider has a stored API key without decrypting it."""
    keys = {
        "anthropic": "llm.anthropic.api_key",
        "openai": "llm.openai.api_key",
        "google": "llm.google.api_key",
    }
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT key, value, encrypted_value FROM user_config
                   WHERE key = ANY($1::text[]) AND user_id IS NULL""",
                list(keys.values()),
            )
    except Exception:
        logger.warning("Could not load cloud provider key presence", exc_info=True)
        return {provider: False for provider in keys}

    row_by_key = {row["key"]: row for row in rows}
    presence: dict[str, bool] = {}
    for provider, config_key in keys.items():
        row = row_by_key.get(config_key)
        presence[provider] = bool(
            row is not None
            and (
                _record_get(row, "encrypted_value") is not None
                or _record_get(row, "value") is not None
            )
        )
    return presence


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

    config: dict[str, Any] = {r["key"]: r["value"] for r in rows}
    setup_completed = _coerce_bool(config.get("setup.completed"), default=False)
    telegram_paired = int(paired_row["n"]) > 0 if paired_row else False
    topics_count = int(topics_row["n"]) if topics_row else 0

    telegram_configured = bool(get_paper_ingestion_settings().telegram_bot_token)

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

_MODEL_ROLES = ("smart", "fast", "embed")


class SystemModelsWithDeliveryResponse(SystemModelsResponse):
    """``SystemModelsResponse`` plus per-role LiteLLM routing truth.

    ``delivery`` maps each role (smart/fast/embed) to ``"applied"`` (LiteLLM
    routes the committed model) or ``"pending_restart"`` (the config row is
    committed but LiteLLM has not yet accepted the update — the reconciler
    keeps retrying until it succeeds). Empty when the delivery state could not
    be read. Additive field; existing consumers of the base shape are unaffected.

    ``routing`` maps each role to the model LiteLLM is *currently* routing
    (the ``litellm_params.model`` string, normalized to strip the provider
    prefix so it compares directly to ``current`` values). Absent / empty when
    LiteLLM is unreachable. Additive; degrades honestly.

    ``consistent`` is True when every role that has a stored ``current`` intent
    also has a matching ``routing`` entry. Roles without a stored intent are
    not considered. False when LiteLLM is unreachable and there is stored
    intent that cannot be verified.
    """

    delivery: dict[str, str] = Field(default_factory=dict)
    routing: dict[str, str] = Field(default_factory=dict)
    consistent: bool = True


async def _load_current_model_assignments(
    db_pool: asyncpg.Pool,
) -> tuple[dict[str, Any], str | None]:
    """Load committed smart/fast/embed model assignments. issue is set on read failure."""
    current: dict[str, Any] = {}
    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT key, value FROM user_config "
                "WHERE key = ANY($1::text[]) AND user_id IS NULL",
                ["llm.smart_model", "llm.fast_model", "llm.embed_model"],
            )
        for r in rows:
            short_key = r["key"].replace("llm.", "")
            val = r["value"]
            # Strip wrapping quotes from JSONB-encoded strings
            if isinstance(val, str):
                val = val.strip('"')
            current[short_key] = val
    except Exception:
        logger.warning("Could not load current model assignments", exc_info=True)
        return current, "Could not load current model assignments."
    return current, None


async def _load_model_delivery_state(db_pool: asyncpg.Pool) -> dict[str, str]:
    """Per-role delivery state: roles in llm.delivery_pending await LiteLLM acceptance."""
    # On read failure delivery stays empty — absence of a claim, never a phantom "applied".
    from paper_ingestion.services.config_write import (  # noqa: PLC0415
        _DELIVERY_PENDING_KEY,
        _fetch_system_config_values,
    )

    try:
        pending_values = await _fetch_system_config_values(db_pool, [_DELIVERY_PENDING_KEY])
        raw_pending = pending_values.get(_DELIVERY_PENDING_KEY)
        pending_roles = {str(r) for r in raw_pending} if isinstance(raw_pending, list) else set()
        return {
            role: "pending_restart" if role in pending_roles else "applied" for role in _MODEL_ROLES
        }
    except Exception:
        logger.warning("Could not load model delivery state", exc_info=True)
        return {}


async def _load_routing_truth(current: dict[str, Any]) -> tuple[dict[str, str], bool]:
    """Read what LiteLLM actually routes now and compare against the committed intent."""
    # Provider prefix is normalized ("ollama/qwen3:8b" → "qwen3:8b") so the comparison
    # is apples-to-apples with how `current` stores model names.
    try:
        from paper_ingestion.services.litellm_config import (  # noqa: PLC0415
            get_litellm_deployments,
        )

        deployments = await get_litellm_deployments()
        # Build a role → routed-model map from the deployment list.
        # Each deployment entry has model_name == alias (smart/fast/embed) and
        # litellm_params.model == the full routed model string (e.g. "ollama/qwen3:8b").
        routing: dict[str, str] = {}
        for dep in deployments:
            alias = dep.model_name
            if alias not in _MODEL_ROLES:
                continue
            params = dep.litellm_params
            routed_full = str(params.get("model", ""))
            if not routed_full:
                continue
            # Normalize: strip provider prefix so "ollama/qwen3:8b" → "qwen3:8b".
            # Cloud models (anthropic/…, openai/…) keep the prefix because that
            # is how `current` stores cloud model assignments too.
            routed_normalized = strip_ollama_prefix(routed_full)
            # Normalize :latest so "qwen3:8b:latest" and "qwen3:8b" compare equal.
            routed_normalized = _strip_latest(routed_normalized)
            # Multiple deployments per alias can exist mid-replace; the
            # reconciler removes stale duplicates on its next pass.
            routing[alias] = routed_normalized

        # Consistency check: every role that has a stored intent must be routing
        # that exact model.  Roles with no stored intent are skipped — no intent
        # means no expectation to violate.  Both sides are :latest-normalized so
        # a direct-API-created row never shows false divergence.
        all_consistent = True
        for role in _MODEL_ROLES:
            role_key = f"{role}_model"
            intent = current.get(role_key)
            if not intent:
                continue
            routed = routing.get(role)
            if _strip_latest(routed or "") != _strip_latest(intent):
                all_consistent = False
        return routing, all_consistent
    except Exception:
        logger.warning("Could not load LiteLLM routing state", exc_info=True)
        # If there is stored intent we cannot verify → not consistent.
        consistent = not any(current.get(f"{role}_model") for role in _MODEL_ROLES)
        return {}, consistent


async def _load_installed_ollama_models(
    http: httpx.AsyncClient, ollama_url: str
) -> tuple[list[dict[str, Any]], str | None]:
    """Fetch installed Ollama models. issue is set on read failure."""
    installed: list[dict[str, Any]] = []
    try:
        resp = await http.get(f"{ollama_url}/api/tags", timeout=10.0)
        if resp.status_code == 200:
            data = resp.json()
            for m in data.get("models", []):
                installed.append(
                    {
                        "name": m.get("name", ""),
                        "size": m.get("size", 0),
                        "parameter_size": m.get("details", {}).get("parameter_size", ""),
                        "quantization": m.get("details", {}).get("quantization_level", ""),
                    }
                )
    except Exception:
        logger.warning("Could not load installed Ollama models", exc_info=True)
        return installed, "Could not load installed Ollama models."
    return installed, None


async def _load_ollama_runtime_count(
    http: httpx.AsyncClient, ollama_url: str
) -> tuple[int | None, str | None]:
    """Fetch the count of currently-loaded Ollama runtime models. issue is set on read failure."""
    try:
        resp = await http.get(f"{ollama_url}/api/ps", timeout=5.0)
        if resp.status_code == 200:
            data = resp.json()
            return len(data.get("models", [])), None
    except Exception:
        logger.warning("Could not load Ollama runtime status", exc_info=True)
        return None, "Could not load Ollama runtime status."
    return None, None


async def _load_num_ctx_overrides(db_pool: asyncpg.Pool, machine_id: str | None) -> dict[str, int]:
    """Fetch per-machine num_ctx overrides so fit_detail reflects the user's chosen context."""
    num_ctx_per_role: dict[str, int] = {}
    if not machine_id:
        return num_ctx_per_role
    num_ctx_keys = [f"llm.{machine_id}.{role}_num_ctx" for role in ("smart", "fast", "embed")]
    try:
        async with db_pool.acquire() as conn:
            num_ctx_rows = await conn.fetch(
                "SELECT key, value FROM user_config "
                "WHERE key = ANY($1::text[]) AND user_id IS NULL",
                num_ctx_keys,
            )
        for row in num_ctx_rows:
            raw_key = row["key"]  # e.g. "llm.host.smart_num_ctx"
            role_part = raw_key.split(".")[-1].replace("_num_ctx", "")
            raw_val = row["value"]
            try:
                num_ctx_per_role[role_part] = int(raw_val)
            except (TypeError, ValueError):
                pass
    except Exception:
        logger.warning("Could not load per-machine num_ctx config", exc_info=True)
    return num_ctx_per_role


async def _get_system_models_data(request: Request) -> SystemModelsWithDeliveryResponse:
    """Inner logic for /models — no auth check; callers must enforce admin gate."""
    ollama_url = get_paper_ingestion_settings().ollama_base_url
    http = request.app.state.http_client
    result: dict[str, Any] = {
        "status": "ok",
        "installed": [],
        "hardware": {},
        "current": {},
        "issues": {},
        "catalog": [],
        "recommendations": {},
    }

    cloud_api_keys: dict[str, bool] = {"anthropic": False, "openai": False, "google": False}
    result["current"], current_issue = await _load_current_model_assignments(
        request.app.state.db_pool
    )
    if current_issue:
        result["issues"]["current"] = current_issue

    result["delivery"] = await _load_model_delivery_state(request.app.state.db_pool)

    result["routing"], result["consistent"] = await _load_routing_truth(result["current"])

    cloud_api_keys = await _cloud_key_presence(request.app.state.db_pool)

    result["installed"], installed_issue = await _load_installed_ollama_models(http, ollama_url)
    if installed_issue:
        result["issues"]["installed"] = installed_issue

    ollama_running, runtime_issue = await _load_ollama_runtime_count(http, ollama_url)
    if ollama_running is not None:
        result["hardware"]["ollama_running"] = ollama_running
    if runtime_issue:
        result["issues"]["runtime"] = runtime_issue

    try:
        validate_embedding_configuration(
            model_name=EMBEDDING_MODEL_NAME,
            dimension=EMBEDDING_DIMENSION,
        )
    except RuntimeError as exc:
        result["issues"]["embedding_config"] = str(exc)

    hardware: HardwareInfo = await async_get_cached_hardware(request.app.state)
    result["hardware"].update(hardware.to_dict())

    num_ctx_per_role = await _load_num_ctx_overrides(request.app.state.db_pool, hardware.machine_id)

    result["catalog"] = build_model_statuses(
        installed=result["installed"],
        current=result["current"],
        embedding_model_name=EMBEDDING_MODEL_NAME,
        hardware=hardware,
        cloud_api_keys=cloud_api_keys,
        num_ctx_per_role=num_ctx_per_role,
    )
    result["recommendations"] = {
        role: recommendations_for_role(
            role,  # type: ignore[arg-type]
            installed=result["installed"],
            current=result["current"],
            embedding_model_name=EMBEDDING_MODEL_NAME,
            hardware=hardware,
            cloud_api_keys=cloud_api_keys,
        )
        for role in ("smart", "fast", "embed")
    }
    # Advisory per-VRAM default-model recommendation.  Convert vram_gb → MiB
    # for recommend_models(); pass None when the probe reported 0 (CPU-only /
    # probe failure) so the None-path safe-default logic fires rather than
    # treating 0 MiB as a concrete GPU measurement.
    vram_mb_for_rec: int | None = round(hardware.vram_gb * 1024) if hardware.vram_gb > 0.0 else None
    hw_rec = recommend_models(vram_mb_for_rec)
    result["hardware_recommendation"] = {
        "vram_mb": hw_rec.vram_mb,
        "bucket": hw_rec.bucket.name,
        "summary": hw_rec.summary,
        "aliases": [
            {
                "alias": a.alias,
                "model": a.model,
                "confirm_on_target": a.confirm_on_target,
                "notes": a.notes,
            }
            for a in hw_rec.aliases
        ],
    }
    result["status"] = "ok" if not result["issues"] else "degraded"
    return SystemModelsWithDeliveryResponse.model_validate(result)


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
    """Return catalog-backed model recommendations for one role."""
    models = await _get_system_models_data(request)
    hardware = await async_get_cached_hardware(request.app.state)
    cloud_api_keys = await _cloud_key_presence(request.app.state.db_pool)
    return {
        "role": role,
        "recommendations": recommendations_for_role(
            role,
            installed=models.installed,
            current=models.current,
            embedding_model_name=EMBEDDING_MODEL_NAME,
            hardware=hardware,
            cloud_api_keys=cloud_api_keys,
        ),
    }


@router.post(
    "/models/{tag:path}/pull",
    dependencies=[Depends(require_admin_or_api_key)],
)
@limiter.limit("3/minute")
async def pull_system_model(
    tag: str,
    request: Request,
    user_id: int | None = Depends(current_user_id),
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
    jarvis_job_id = str(uuid.uuid4())
    await task.defer_async(
        job_id=jarvis_job_id,
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
    return JobCreateResponse(job_id=jarvis_job_id, status="queued")


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
            detail="Cannot verify active model assignments; refusing to delete model",
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

# Free-space floor for the `pressure` flag: the smallest documented image-pull
# budget (scripts/setup_lib.sh _image_budget_gb "cpu-pull" = 6 GB) — below this,
# even a routine registry pull is likely to fail.
_LOW_DISK_FREE_GB = 6.0

# Mirrors the `hf_cache` volume mount in docker-compose.yml (persists Docling's
# HuggingFace layout/table models across container recreation). This is the
# only writable, app-owned mount available to this zero-privilege container
# (no postgres_data / qdrant_data mount), so it also doubles as the
# disk-pressure probe target below.
_HF_CACHE_DIR = Path("/tmp/hf_cache")


class StorageSection(BaseModel):
    """One storage backend's usage. ``bytes_used`` is None when the size is
    unknown: either the backend was unreachable (``error`` set) or it has no
    byte-level size API (Qdrant — see ``qdrant_collections`` for its proxy).
    """

    bytes_used: int | None = None
    error: str | None = None


class QdrantCollectionUsage(BaseModel):
    """Per-collection point count — the closest usage signal qdrant-client
    1.17.1 exposes; it has no per-collection byte-size API.
    """

    name: str
    points_count: int | None = None


class StorageResponse(BaseModel):
    """Disk-usage snapshot for admins: GET /api/system/storage.

    Every section degrades to a null/empty state on failure rather than
    5xx-ing the whole request — one unreachable backend must never hide the
    usage story for the others.
    """

    ollama_models: StorageSection
    postgres: StorageSection
    qdrant: StorageSection
    qdrant_collections: list[QdrantCollectionUsage] = Field(default_factory=list)
    hf_cache: StorageSection
    pressure: bool


def _dir_size_bytes(path: Path) -> int:
    """Recursive directory byte size (a pure-Python ``du -sb``)."""
    total = 0
    for entry in path.rglob("*"):
        if entry.is_file():
            try:
                total += entry.stat().st_size
            except OSError:
                continue
    return total


async def _ollama_storage_usage(request: Request) -> StorageSection:
    http = request.app.state.http_client
    ollama_url = get_paper_ingestion_settings().ollama_base_url
    installed, issue = await _load_installed_ollama_models(http, ollama_url)
    if issue:
        return StorageSection(error=issue)
    return StorageSection(bytes_used=sum(m.get("size", 0) for m in installed))


async def _postgres_storage_usage(pool: asyncpg.Pool) -> StorageSection:
    try:
        async with pool.acquire() as conn:
            size = await conn.fetchval("SELECT pg_database_size(current_database())")
        return StorageSection(bytes_used=int(size) if size is not None else None)
    except Exception as exc:  # noqa: BLE001 — surface class name, not raw message
        logger.warning("storage: postgres size probe failed", exc_info=True)
        return StorageSection(error=type(exc).__name__)


async def _qdrant_storage_usage(
    qdrant: Any | None,
) -> tuple[StorageSection, list[QdrantCollectionUsage]]:
    """Qdrant exposes no per-collection byte size; point counts are the proxy."""
    if qdrant is None:
        return StorageSection(error="Qdrant client not available"), []
    try:
        collections = await qdrant.get_collections()
        usages = [
            QdrantCollectionUsage(
                name=c.name,
                points_count=(await qdrant.get_collection(c.name)).points_count,
            )
            for c in collections.collections
        ]
        return StorageSection(), usages
    except Exception as exc:  # noqa: BLE001 — surface class name, not raw message
        logger.warning("storage: qdrant probe failed", exc_info=True)
        return StorageSection(error=type(exc).__name__), []


def _hf_cache_storage_usage() -> StorageSection:
    if not _HF_CACHE_DIR.is_dir():
        return StorageSection(bytes_used=0)
    try:
        return StorageSection(bytes_used=_dir_size_bytes(_HF_CACHE_DIR))
    except OSError as exc:
        logger.warning("storage: hf_cache probe failed", exc_info=True)
        return StorageSection(error=type(exc).__name__)


def _disk_pressure() -> bool:
    """True when free space on the hf_cache volume drops below the safe floor."""
    try:
        free_gb = shutil.disk_usage(_HF_CACHE_DIR).free / 1e9
    except OSError:
        return False
    return free_gb < _LOW_DISK_FREE_GB


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
        ollama_models=await _ollama_storage_usage(request),
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
