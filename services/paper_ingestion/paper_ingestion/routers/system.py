"""System status endpoints: setup wizard readiness + Ollama model info."""

import importlib.util
import logging
import re
import time
import uuid
from typing import Any, Literal

import asyncpg
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from jarvis_common import (
    JobCreateResponse,
    current_user_id,
    require_admin,
    require_admin_or_api_key,
)
from jarvis_common.audit import log_audit
from jarvis_common.hardware_fit import recommend_models
from jarvis_common.model_catalog import Role
from jarvis_common.serialization import _coerce_bool
from jarvis_common.settings import get_core_settings, get_secrets_settings
from pydantic import BaseModel, Field

from paper_ingestion.config import get_paper_ingestion_settings
from paper_ingestion.deps import get_db_pool, limiter
from paper_ingestion.ingestion.embedder import (
    EMBEDDING_DIMENSION,
    EMBEDDING_MODEL_NAME,
    validate_embedding_configuration,
)
from paper_ingestion.models import SystemModelsResponse
from paper_ingestion.services.model_lifecycle import (
    HardwareInfo,
    async_get_cached_hardware,
    build_model_statuses,
    catalog_entry_for_model,
    normalize_model_tag,
    recommendations_for_role,
)

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

    warnings: list[str] = []
    try:
        from paper_ingestion.services.litellm_config import (  # noqa: PLC0415
            get_litellm_deployments,
        )

        deployments = await get_litellm_deployments()
    except Exception:
        logger.debug("model_warnings: LiteLLM probe failed — skipping", exc_info=True)
        _model_warnings_cache.set(now, [])
        return []

    # Build role → routed-model map (ollama/ prefix stripped + :latest stripped).
    role_to_routed: dict[str, str] = {}
    for dep in deployments:
        alias = dep.get("model_name", "")
        if alias not in _MODEL_ROLES:
            continue
        params = dep.get("litellm_params") or {}
        routed_full = str(params.get("model", ""))
        if not routed_full:
            continue
        if routed_full.startswith("ollama/"):
            routed_full = routed_full[len("ollama/") :]
        role_to_routed[alias] = _strip_latest(routed_full)

    if not role_to_routed:
        _model_warnings_cache.set(now, [])
        return []

    # Fetch installed Ollama models.
    ollama_url = get_paper_ingestion_settings().ollama_base_url
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{ollama_url}/api/tags")
        if resp.status_code != 200:
            # Reachable-but-erroring Ollama (e.g. 503 during startup): log the
            # status so it is greppable, then degrade to no warnings.
            logger.debug(
                "model_warnings: Ollama /api/tags returned %s — skipping", resp.status_code
            )
            _model_warnings_cache.set(now, [])
            return []
        data = resp.json()
        installed: set[str] = {_strip_latest(m.get("name", "")) for m in data.get("models", [])}
    except Exception:
        logger.debug("model_warnings: Ollama probe failed — skipping", exc_info=True)
        _model_warnings_cache.set(now, [])
        return []

    # Emit a warning for each Ollama role that routes a model not yet pulled.
    # Cloud models (containing "/") are skipped — pulled check is Ollama-specific.
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
    try:
        async with request.app.state.db_pool.acquire() as conn:
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
            result["current"][short_key] = val
    except Exception:
        logger.warning("Could not load current model assignments", exc_info=True)
        result["issues"]["current"] = "Could not load current model assignments."

    # Per-role delivery state (CRIT-1): roles listed in llm.delivery_pending have
    # a committed config row that LiteLLM has not accepted yet. On read failure
    # delivery stays empty — absence of a claim, never a phantom "applied".
    from paper_ingestion.services.config_write import (  # noqa: PLC0415
        _DELIVERY_PENDING_KEY,
        _fetch_system_config_values,
    )

    result["delivery"] = {}
    try:
        pending_values = await _fetch_system_config_values(
            request.app.state.db_pool, [_DELIVERY_PENDING_KEY]
        )
        raw_pending = pending_values.get(_DELIVERY_PENDING_KEY)
        pending_roles = {str(r) for r in raw_pending} if isinstance(raw_pending, list) else set()
        result["delivery"] = {
            role: "pending_restart" if role in pending_roles else "applied" for role in _MODEL_ROLES
        }
    except Exception:
        logger.warning("Could not load model delivery state", exc_info=True)

    # Routing truth (T1.3): read what LiteLLM is *actually* routing right now
    # and compare against the committed DB intent in `current`.  Normalize the
    # provider prefix (e.g. "ollama/qwen3:8b" → "qwen3:8b") so the comparison
    # is apples-to-apples with how `current` stores model names.
    result["routing"] = {}
    result["consistent"] = True
    try:
        from paper_ingestion.services.litellm_config import (  # noqa: PLC0415
            get_litellm_deployments,
        )

        deployments = await get_litellm_deployments()
        # Build a role → routed-model map from the deployment list.
        # Each deployment entry has model_name == alias (smart/fast/embed) and
        # litellm_params.model == the full routed model string (e.g. "ollama/qwen3:8b").
        for dep in deployments:
            alias = dep.get("model_name", "")
            if alias not in _MODEL_ROLES:
                continue
            params = dep.get("litellm_params") or {}
            routed_full = str(params.get("model", ""))
            if not routed_full:
                continue
            # Normalize: strip provider prefix so "ollama/qwen3:8b" → "qwen3:8b".
            # Cloud models (anthropic/…, openai/…) keep the prefix because that
            # is how `current` stores cloud model assignments too.
            if routed_full.startswith("ollama/"):
                routed_normalized = routed_full[len("ollama/") :]
            else:
                routed_normalized = routed_full
            # Normalize :latest so "qwen3:8b:latest" and "qwen3:8b" compare equal.
            routed_normalized = _strip_latest(routed_normalized)
            # Multiple deployments per alias can exist mid-replace; the
            # reconciler removes stale duplicates on its next pass.
            result["routing"][alias] = routed_normalized

        # Consistency check: every role that has a stored intent must be routing
        # that exact model.  Roles with no stored intent are skipped — no intent
        # means no expectation to violate.  Both sides are :latest-normalized so
        # a direct-API-created row never shows false divergence.
        stored_current: dict[str, str] = result.get("current") or {}
        all_consistent = True
        for role in _MODEL_ROLES:
            role_key = f"{role}_model"
            intent = stored_current.get(role_key)
            if not intent:
                continue
            routed = result["routing"].get(role)
            if _strip_latest(routed or "") != _strip_latest(intent):
                all_consistent = False
        result["consistent"] = all_consistent
    except Exception:
        logger.warning("Could not load LiteLLM routing state", exc_info=True)
        result["routing"] = {}
        # If there is stored intent we cannot verify → not consistent.
        stored_current = result.get("current") or {}
        result["consistent"] = not any(stored_current.get(f"{role}_model") for role in _MODEL_ROLES)

    cloud_api_keys = await _cloud_key_presence(request.app.state.db_pool)

    try:
        resp = await http.get(f"{ollama_url}/api/tags", timeout=10.0)
        if resp.status_code == 200:
            data = resp.json()
            for m in data.get("models", []):
                result["installed"].append(
                    {
                        "name": m.get("name", ""),
                        "size": m.get("size", 0),
                        "parameter_size": m.get("details", {}).get("parameter_size", ""),
                        "quantization": m.get("details", {}).get("quantization_level", ""),
                    }
                )
    except Exception:
        logger.warning("Could not load installed Ollama models", exc_info=True)
        result["issues"]["installed"] = "Could not load installed Ollama models."

    try:
        resp = await http.get(f"{ollama_url}/api/ps", timeout=5.0)
        if resp.status_code == 200:
            data = resp.json()
            result["hardware"]["ollama_running"] = len(data.get("models", []))
    except Exception:
        logger.warning("Could not load Ollama runtime status", exc_info=True)
        result["issues"]["runtime"] = "Could not load Ollama runtime status."

    try:
        validate_embedding_configuration(
            model_name=EMBEDDING_MODEL_NAME,
            dimension=EMBEDDING_DIMENSION,
        )
    except RuntimeError as exc:
        result["issues"]["embedding_config"] = str(exc)

    hardware: HardwareInfo = await async_get_cached_hardware(request.app.state)
    result["hardware"].update(hardware.to_dict())

    # Fetch per-machine num_ctx overrides so fit_detail reflects the user's chosen context.
    num_ctx_per_role: dict[str, int] = {}
    machine_id = hardware.machine_id
    if machine_id:
        num_ctx_keys = [f"llm.{machine_id}.{role}_num_ctx" for role in ("smart", "fast", "embed")]
        try:
            async with request.app.state.db_pool.acquire() as conn:
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
# GET /api/system/readiness
# ---------------------------------------------------------------------------

ReadinessStatus = Literal["green", "amber", "red"]

# Ordered worst-to-best so aggregate selection is a simple max-by-rank.
_STATUS_RANK: dict[str, int] = {"green": 0, "amber": 1, "red": 2}


class ReadinessCheck(BaseModel):
    """One pre-public-launch readiness probe result."""

    name: str
    status: ReadinessStatus
    detail: str
    remediation: str = ""


class ReadinessResponse(BaseModel):
    """Aggregate readiness report. ``status`` is the worst of ``checks``."""

    status: ReadinessStatus
    checks: list[ReadinessCheck]


# Granular dev-flag attribute names on CoreSettings. Each must be False for a
# production deployment; True means a safety bypass is active → red.
_DEV_FLAG_NAMES: tuple[str, ...] = (
    "dev_auth_bypass",
    "dev_error_detail",
    "dev_cors_open",
    "dev_smtp_log_only",
    "dev_crypto_relaxed",
)


@router.get(
    "/readiness",
    response_model=ReadinessResponse,
    dependencies=[Depends(require_admin_or_api_key)],
)
@limiter.limit("30/minute")
async def get_system_readiness(request: Request) -> ReadinessResponse:
    """Pre-public-launch readiness report: dev flags, env, secrets, HTTPS, audit log."""
    core = get_core_settings()
    secrets = get_secrets_settings()
    checks: list[ReadinessCheck] = []

    # Granular dev flags — each must be off for production.
    # Per-check remediation strings (what to set + concrete risk).
    _dev_remediation: dict[str, str] = {
        "dev_auth_bypass": (
            "Set DEV_AUTH_BYPASS=false and DEV_MODE=false before sharing this URL — "
            "anyone who can reach this page can sign in as any user."
        ),
        "dev_error_detail": (
            "Set DEV_ERROR_DETAIL=false in production — full error tracebacks leak "
            "internal file paths and logic to anyone who triggers an error."
        ),
        "dev_cors_open": (
            "Set DEV_CORS_OPEN=false and restrict CORS_ORIGINS to your domain before "
            "going live — otherwise any website can silently act on behalf of a user."
        ),
        "dev_smtp_log_only": (
            "Set DEV_SMTP_LOG_ONLY=false and configure SMTP credentials (SMTP_HOST, "
            "SMTP_USER, SMTP_PASS) for production — otherwise sign-in emails print to "
            "logs and users never receive them."
        ),
        "dev_crypto_relaxed": (
            "Set DEV_CRYPTO_RELAXED=false in production — login tokens use weaker "
            "security and stay valid longer if stolen."
        ),
    }
    for flag_name in _DEV_FLAG_NAMES:
        enabled = bool(getattr(core, flag_name))
        checks.append(
            ReadinessCheck(
                name=flag_name,
                status="red" if enabled else "green",
                detail="enabled" if enabled else "disabled",
                remediation=_dev_remediation.get(flag_name, ""),
            )
        )

    # Environment.
    env_value = core.environment
    checks.append(
        ReadinessCheck(
            name="environment",
            status="green" if env_value == "production" else "amber",
            detail=env_value,
            remediation=(
                "Set ENVIRONMENT=production before going live — some safeguards "
                "(rate-limits, security headers) only activate in production mode."
            ),
        )
    )

    # API key — presence + minimum length, never echoed.
    api_key = secrets.jarvis_api_key
    if api_key is None:
        checks.append(
            ReadinessCheck(
                name="api_key",
                status="red",
                detail="missing",
                remediation=(
                    "Generate a 32-byte key with: openssl rand -hex 32. "
                    "Then set JARVIS_API_KEY to that value."
                ),
            )
        )
    elif len(api_key.get_secret_value()) >= 32:
        checks.append(
            ReadinessCheck(
                name="api_key",
                status="green",
                detail="configured (>=32 chars)",
                remediation="",
            )
        )
    else:
        checks.append(
            ReadinessCheck(
                name="api_key",
                status="red",
                detail="too short",
                remediation=(
                    "Generate a new 32-byte key with: openssl rand -hex 32. "
                    "Then set JARVIS_API_KEY to that value."
                ),
            )
        )

    # SMTP — magic links fall back to stdout when unset.
    smtp_configured = secrets.smtp_host is not None
    checks.append(
        ReadinessCheck(
            name="smtp",
            status="green" if smtp_configured else "amber",
            detail=(
                "configured" if smtp_configured else "not configured — magic links go to stdout"
            ),
            remediation=(
                "Configure SMTP_HOST, SMTP_USER, SMTP_PASS, and SMTP_FROM "
                "before inviting real users — otherwise sign-in emails print to logs."
            ),
        )
    )

    # HTTPS — inferred from the request / proxy header (no settings field).
    forwarded_proto = request.headers.get("x-forwarded-proto", "").lower()
    scheme = forwarded_proto or request.url.scheme
    checks.append(
        ReadinessCheck(
            name="https",
            status="green" if scheme == "https" else "amber",
            detail=scheme,
            remediation=(
                "Ensure TLS is terminated at the edge — Caddy/nginx handles this "
                "automatically when pointed at a real domain."
            ),
        )
    )

    # Audit log — count rows; informational only.
    try:
        async with request.app.state.db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT COUNT(*) AS n FROM audit_log")
        n = row["n"] if row is not None else 0
        checks.append(
            ReadinessCheck(
                name="audit_log",
                status="green",
                detail=f"{n} rows",
                remediation="",
            )
        )
    except Exception as exc:  # noqa: BLE001 — surface class name, not raw message
        checks.append(
            ReadinessCheck(
                name="audit_log",
                status="amber",
                detail=type(exc).__name__,
                remediation="The audit_log table could not be queried. Check connectivity.",
            )
        )

    aggregate = max(checks, key=lambda c: _STATUS_RANK[c.status]).status
    return ReadinessResponse(status=aggregate, checks=checks)


# ---------------------------------------------------------------------------
# GET /api/system/capabilities
# ---------------------------------------------------------------------------


class SystemCapabilities(BaseModel):
    """Available optional heavy-library capabilities on the backend."""

    networkx: bool
    scikit_learn: bool


@router.get(
    "/capabilities",
    response_model=SystemCapabilities,
    dependencies=[Depends(require_admin_or_api_key)],
)
@limiter.limit("30/minute")
async def get_system_capabilities(request: Request) -> SystemCapabilities:
    """Return whether optional heavy libraries (networkx, scikit-learn) are importable.

    Uses ``importlib.util.find_spec`` — no actual import, trivially cheap.
    The frontend Pulse settings UI uses this to suppress false-alarm warnings.
    """
    return SystemCapabilities(
        networkx=importlib.util.find_spec("networkx") is not None,
        scikit_learn=importlib.util.find_spec("sklearn") is not None,
    )
