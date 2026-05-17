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
from jarvis_common import JobCreateResponse, current_user_id, require_admin_or_api_key
from jarvis_common.hardware_fit import recommend_models
from jarvis_common.model_catalog import Role
from jarvis_common.serialization import _coerce_bool
from jarvis_common.settings import get_core_settings, get_secrets_settings
from pydantic import BaseModel

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

# Ollama models expected after first-run provisioning. We match by name prefix
# so either "qwen3:4b" or "qwen3:4b-instruct" style names count as installed.
_EXPECTED_MODEL_PREFIXES: tuple[str, ...] = (
    "qwen3:14b",
    "qwen3:4b",
    "qwen3-embedding",
)

_OLLAMA_DEFAULT_BASE_URL = "http://ollama:11434"
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


class SetupStatus(BaseModel):
    setup_completed: bool
    models_ready: bool
    models_downloading: list[str]
    topics_count: int
    telegram_configured: bool
    telegram_paired: bool


def _is_owner_chat_paired(value: Any) -> bool:
    """Return True iff ``telegram.owner_chat_id`` contains a real chat id."""
    if value is None:
        return False
    if isinstance(value, str) and value.lower() == "null":
        return False
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return False


def _record_get(row: Any, key: str) -> Any:
    if hasattr(row, "get"):
        return row.get(key)
    try:
        return row[key]
    except Exception:
        return None


def _models_match(installed_names: list[str]) -> bool:
    """Return True iff every expected model prefix is present in installed."""
    if not installed_names:
        return False
    for expected in _EXPECTED_MODEL_PREFIXES:
        if not any(name.startswith(expected) for name in installed_names):
            return False
    return True


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
            result: tuple[bool, list[str]] = (False, [])
            _ollama_probe_cache.set(now, result)
            return result
        data = resp.json()
        installed = [m.get("name", "") for m in data.get("models", [])]
        result = (_models_match(installed), [])
    except Exception:
        logger.warning("setup-status: Ollama probe failed", exc_info=True)
        result = (False, [])
    _ollama_probe_cache.set(now, result)
    return result


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


@router.get("/setup-status", response_model=SetupStatus)
@limiter.limit("30/minute")
async def get_setup_status(
    request: Request,
    pool: asyncpg.Pool = Depends(get_db_pool),
) -> SetupStatus:
    """Return a point-in-time snapshot of setup wizard readiness signals."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT key, value FROM user_config WHERE key = ANY($1::text[]) AND user_id IS NULL",
            ["setup.completed", "telegram.owner_chat_id"],
        )
        topics_row = await conn.fetchrow("SELECT COUNT(*) AS n FROM topics")

    config: dict[str, Any] = {r["key"]: r["value"] for r in rows}
    setup_completed = _coerce_bool(config.get("setup.completed"), default=False)
    telegram_paired = _is_owner_chat_paired(config.get("telegram.owner_chat_id"))
    topics_count = int(topics_row["n"]) if topics_row else 0

    telegram_configured = bool(get_paper_ingestion_settings().telegram_bot_token)

    models_ready, models_downloading = await _probe_ollama()

    return SetupStatus(
        setup_completed=setup_completed,
        models_ready=models_ready,
        models_downloading=models_downloading,
        topics_count=topics_count,
        telegram_configured=telegram_configured,
        telegram_paired=telegram_paired,
    )


# ---------------------------------------------------------------------------
# GET /api/system/models
# ---------------------------------------------------------------------------


@router.get(
    "/models",
    response_model=SystemModelsResponse,
    dependencies=[Depends(require_admin_or_api_key)],
)
@limiter.limit("30/minute")
async def get_system_models(request: Request) -> SystemModelsResponse:
    """Return installed Ollama models + hardware info + current assignments."""
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
    return SystemModelsResponse.model_validate(result)


@router.get("/hardware")
@limiter.limit("10/minute")
async def get_system_hardware(request: Request) -> dict[str, Any]:
    """Return local accelerator information for model selection."""
    return (await async_get_cached_hardware(request.app.state)).to_dict()


@router.get("/models/recommendations")
@limiter.limit("30/minute")
async def get_model_recommendations(
    request: Request,
    role: Role = "smart",
) -> dict[str, Any]:
    """Return catalog-backed model recommendations for one role."""
    models = await get_system_models(request)
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


@router.post("/models/{tag:path}/pull")
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
    return JobCreateResponse(job_id=jarvis_job_id, status="queued")


@router.delete("/models/{tag:path}", status_code=204, response_class=Response)
@limiter.limit("3/minute")
async def delete_system_model(tag: str, request: Request) -> Response:
    """Delete an inactive Ollama model tag."""
    if not _TAG_RE.match(tag):
        raise HTTPException(status_code=422, detail="Invalid model tag format")
    entry = catalog_entry_for_model(tag)
    if entry is None or entry.provider != "ollama":
        raise HTTPException(status_code=404, detail="Unknown local catalog model")

    models = await get_system_models(request)
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
