"""System status endpoints: setup wizard readiness + Ollama model info."""

from __future__ import annotations

import logging
import os
import re
import time
import uuid
from typing import Any

import asyncpg
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from jarvis_common import JobCreateResponse, current_user_id
from pydantic import BaseModel

from paper_ingestion.deps import get_db_pool, limiter
from paper_ingestion.ingestion.embedder import (
    EMBEDDING_DIMENSION,
    EMBEDDING_MODEL_NAME,
    validate_embedding_configuration,
)
from paper_ingestion.models import SystemModelsResponse
from paper_ingestion.services.model_lifecycle import (
    HardwareInfo,
    Role,
    build_model_statuses,
    catalog_entry_for_model,
    get_cached_hardware,
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


def _coerce_bool(value: Any, default: bool = False) -> bool:
    """Interpret user_config JSONB values as booleans."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.lower()
        if lowered in ("true", "1", "yes"):
            return True
        if lowered in ("false", "0", "no", "null", ""):
            return False
    return bool(value)


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

    ollama_url = os.environ.get("OLLAMA_BASE_URL", _OLLAMA_DEFAULT_BASE_URL)
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
                "SELECT key, value, encrypted_value FROM user_config WHERE key = ANY($1::text[])",
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
            "SELECT key, value FROM user_config WHERE key = ANY($1::text[])",
            ["setup.completed", "telegram.owner_chat_id"],
        )
        topics_row = await conn.fetchrow("SELECT COUNT(*) AS n FROM topics")

    config: dict[str, Any] = {r["key"]: r["value"] for r in rows}
    setup_completed = _coerce_bool(config.get("setup.completed"), default=False)
    telegram_paired = _is_owner_chat_paired(config.get("telegram.owner_chat_id"))
    topics_count = int(topics_row["n"]) if topics_row else 0

    telegram_configured = bool(os.environ.get("TELEGRAM_BOT_TOKEN"))

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


@router.get("/models", response_model=SystemModelsResponse)
@limiter.limit("30/minute")
async def get_system_models(request: Request) -> SystemModelsResponse:
    """Return installed Ollama models + hardware info + current assignments."""
    ollama_url = os.environ.get("OLLAMA_BASE_URL", "http://ollama:11434")
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
                "SELECT key, value FROM user_config WHERE key = ANY($1::text[])",
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

    hardware: HardwareInfo = get_cached_hardware(request.app.state)
    result["hardware"].update(hardware.to_dict())
    result["catalog"] = build_model_statuses(
        installed=result["installed"],
        current=result["current"],
        embedding_model_name=EMBEDDING_MODEL_NAME,
        hardware=hardware,
        cloud_api_keys=cloud_api_keys,
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
    result["status"] = "ok" if not result["issues"] else "degraded"
    return SystemModelsResponse.model_validate(result)


@router.get("/hardware")
@limiter.limit("10/minute")
async def get_system_hardware(request: Request) -> dict[str, Any]:
    """Return local accelerator information for model selection."""
    return get_cached_hardware(request.app.state).to_dict()


@router.get("/models/recommendations")
@limiter.limit("30/minute")
async def get_model_recommendations(
    request: Request,
    role: Role = "smart",
) -> dict[str, Any]:
    """Return catalog-backed model recommendations for one role."""
    models = await get_system_models(request)
    hardware = get_cached_hardware(request.app.state)
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
        ollama_url=os.environ.get("OLLAMA_BASE_URL", "http://ollama:11434"),
    )
    return JobCreateResponse(job_id=jarvis_job_id, status="queued")


@router.delete("/models/{tag:path}", status_code=204, response_class=Response)
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
    ollama_url = os.environ.get("OLLAMA_BASE_URL", "http://ollama:11434")
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
