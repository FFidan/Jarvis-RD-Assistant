"""Admin-only AI backend configuration endpoints."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from jarvis_common.auth import require_admin
from jarvis_common.hw_detect import detect_tier
from jarvis_common.litellm_observer import observed_share
from pydantic import BaseModel, Field

from paper_ingestion.config import get_paper_ingestion_settings
from paper_ingestion.deps import get_db_pool
from paper_ingestion.services.ai_settings import (
    AISettingsApplier,
    candidate_is_allowed,
    find_candidate_config_path,
    resolve_candidates_for_tier,
)
from paper_ingestion.services.model_lifecycle import normalize_model_tag
from paper_ingestion.services.model_prefixes import strip_ollama_prefix

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings/ai", tags=["settings"])

# Allow the session-based admin dependency without requiring X-API-Key.
# Mark as session-exempt so main.py can register with dependencies=[].
router.auth_exempt = True  # type: ignore[attr-defined]

_CONFIG_PATH = find_candidate_config_path()
_APPLIER = AISettingsApplier()


class AISettingsResponse(BaseModel):
    hw_tier: str
    recommended_backend: str
    recommended_model: str
    configured_backend: str | None
    configured_model: str | None
    observed_backend: str | None
    observed_recent_share: float
    candidates_for_tier: list[dict[str, Any]]
    candidate_issues: list[str] = Field(default_factory=list)
    eval_report_date: str | None


class ApplyRequest(BaseModel):
    backend: str = Field(max_length=64)
    model: str = Field(max_length=128)


class DismissRequest(BaseModel):
    banner_kind: str = Field(max_length=64)


def _effective_tier() -> str:
    """Return JARVIS_HW_TIER env override if set, else live detect_tier()."""
    return os.getenv("JARVIS_HW_TIER") or detect_tier()


async def _ensure_ollama_model_present(
    client: httpx.AsyncClient, ollama_url: str, model: str
) -> None:
    """Ensure ``model`` is installed in Ollama, pulling it if absent.

    Raises ``RuntimeError`` on any failure (unreachable, pull error, bad
    status, or stream error event). The caller maps this to a generic 502
    so provider internals are never leaked to the API consumer.
    """
    target = normalize_model_tag(model)

    tags_resp = await client.get(f"{ollama_url}/api/tags", timeout=30.0)
    tags_resp.raise_for_status()
    installed = {
        normalize_model_tag(str(entry.get("name", "")))
        for entry in tags_resp.json().get("models", [])
    }
    if target in installed:
        return

    # Strip the LiteLLM ``ollama/`` / ``ollama_chat/`` prefix for the pull name;
    # Ollama expects the bare tag (e.g. ``qwen3:8b``).
    pull_name = strip_ollama_prefix(model)
    async with client.stream(
        "POST",
        f"{ollama_url}/api/pull",
        json={"name": pull_name, "stream": True},
        timeout=None,
    ) as resp:
        if resp.status_code >= 400:
            raise RuntimeError(f"Ollama pull returned status {resp.status_code}")
        # Success is a POSITIVE terminal event ({"status":"success"}), not stream
        # exhaustion — a truncated/dropped stream must NOT be read as a complete
        # pull (that would route to a partially-downloaded model).
        saw_success = False
        async for line in resp.aiter_lines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("ollama pull: non-JSON line for %s: %r", model, line[:200])
                continue
            if event.get("error"):
                raise RuntimeError(str(event["error"]))
            if event.get("status") == "success":
                saw_success = True
    if not saw_success:
        raise RuntimeError(f"Ollama pull for {model!r} ended without a success event")


@router.get("", response_model=AISettingsResponse)
async def get_ai_settings(_admin: None = Depends(require_admin)) -> AISettingsResponse:
    tier = _effective_tier()
    selection = resolve_candidates_for_tier(tier, config_path=_CONFIG_PATH)
    top = selection.recommended
    served, share = observed_share("smart")
    return AISettingsResponse(
        hw_tier=tier,
        recommended_backend=top["backend"],
        recommended_model=top["model"],
        configured_backend=os.getenv("JARVIS_LLM_BACKEND"),
        configured_model=os.getenv("JARVIS_SMART_MODEL"),
        observed_backend=served,
        observed_recent_share=share,
        candidates_for_tier=selection.candidates,
        candidate_issues=selection.issues,
        eval_report_date=selection.generated_from,
    )


@router.post("", response_model=AISettingsResponse)
async def apply_ai_settings(
    req: ApplyRequest,
    request: Request,
    _admin: None = Depends(require_admin),
) -> AISettingsResponse:
    tier = _effective_tier()
    selection = resolve_candidates_for_tier(tier, config_path=_CONFIG_PATH)
    if not candidate_is_allowed(selection, backend=req.backend, model=req.model):
        raise HTTPException(
            422, "backend/model is not an allowed candidate for current hardware tier"
        )

    # For Ollama, ensure the target model is pulled BEFORE mutating any env /
    # LiteLLM config. If the pull fails, the previous config is left untouched
    # so LiteLLM never routes to a missing model. vLLM/other backends are
    # served externally and skip this check.
    if req.backend == "ollama":
        ollama_url = get_paper_ingestion_settings().ollama_base_url.rstrip("/")
        client: httpx.AsyncClient = request.app.state.http_client
        try:
            await _ensure_ollama_model_present(client, ollama_url, req.model)
        except Exception as exc:
            logger.exception(
                "settings_ai ollama model pull/validate failed",
                extra={"backend": req.backend, "model": req.model},
            )
            raise HTTPException(
                502,
                f"Ollama model pull failed for {req.model!r}; previous config unchanged",
            ) from exc

    try:
        _APPLIER.apply(backend=req.backend, model=req.model, tier=tier)
    except Exception as exc:
        # str(exc) would reflect provider/admin-push internals to the API
        # consumer; log server-side and surface a generic 502.
        logger.exception(
            "settings_ai apply failed", extra={"backend": req.backend, "model": req.model}
        )
        raise HTTPException(502, "apply failed; previous config restored") from exc

    return await get_ai_settings()


@router.post("/redetect", response_model=AISettingsResponse)
async def redetect_hw(_admin: None = Depends(require_admin)) -> AISettingsResponse:
    return await get_ai_settings()


@router.post("/dismiss-banner")
async def dismiss_banner(
    req: DismissRequest,
    admin: None = Depends(require_admin),
    pool=Depends(get_db_pool),
) -> dict:
    # admin dependency returns None (raises 403 on failure); user_id not
    # available from require_admin — omit from context rather than invent.
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO system_events (level, category, source, message, context)
            VALUES ($1, $2, $3, $4, $5::jsonb)
            """,
            "info",
            "config",
            "settings_ai",
            f"banner dismissed: {req.banner_kind}",
            {"banner_kind": req.banner_kind},
        )
    return {"ok": True}
