"""Admin-only AI backend configuration endpoints."""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, Depends, HTTPException
from jarvis_common.hw_detect import detect_tier
from jarvis_common.litellm_observer import observed_share
from pydantic import BaseModel

from paper_ingestion.deps import get_db_pool
from paper_ingestion.routers.admin import require_admin

router = APIRouter(prefix="/api/settings/ai", tags=["settings"])

# Allow the session-based admin dependency without requiring X-API-Key.
# Mark as session-exempt so main.py can register with dependencies=[].
router.auth_exempt = True  # type: ignore[attr-defined]


def _find_config_path() -> Path:
    """Walk up from this file looking for config/llm-tier-candidates.yaml.

    Works on both host (.../services/paper_ingestion/paper_ingestion/routers/)
    and inside the docker container (/app/paper_ingestion/routers/ — relies on
    a `./config:/app/config:ro` bind-mount in docker-compose.yml).
    """
    for ancestor in Path(__file__).resolve().parents:
        candidate = ancestor / "config" / "llm-tier-candidates.yaml"
        if candidate.exists():
            return candidate
    return Path("/app/config/llm-tier-candidates.yaml")


_CONFIG_PATH = _find_config_path()


class AISettingsResponse(BaseModel):
    hw_tier: str
    recommended_backend: str
    recommended_model: str
    configured_backend: str | None
    configured_model: str | None
    observed_backend: str | None
    observed_recent_share: float
    candidates_for_tier: list[dict[str, Any]]
    eval_report_date: str | None


class ApplyRequest(BaseModel):
    backend: str
    model: str


class DismissRequest(BaseModel):
    banner_kind: str


def _load_candidates() -> dict:
    if not _CONFIG_PATH.exists():
        return {"tiers": {}}
    with _CONFIG_PATH.open() as f:
        return yaml.safe_load(f) or {"tiers": {}}


def _effective_tier() -> str:
    """Return JARVIS_HW_TIER env override if set, else live detect_tier()."""
    return os.getenv("JARVIS_HW_TIER") or detect_tier()


@router.get("", response_model=AISettingsResponse)
async def get_ai_settings(_admin: None = Depends(require_admin)) -> AISettingsResponse:
    tier = _effective_tier()
    data = _load_candidates()
    tier_entry = data.get("tiers", {}).get(tier, {})
    candidates = tier_entry.get("candidates", [])
    top = candidates[0] if candidates else {"backend": "ollama", "model": "qwen3:1.7b"}
    served, share = observed_share("smart")
    return AISettingsResponse(
        hw_tier=tier,
        recommended_backend=top["backend"],
        recommended_model=top["model"],
        configured_backend=os.getenv("JARVIS_LLM_BACKEND"),
        configured_model=os.getenv("JARVIS_SMART_MODEL"),
        observed_backend=served,
        observed_recent_share=share,
        candidates_for_tier=candidates,
        eval_report_date=data.get("generated_from"),
    )


def _patch_env_file(updates: dict[str, str]) -> None:
    env_path = Path(".env")
    if not env_path.exists():
        return
    lines = env_path.read_text().splitlines()
    seen: set[str] = set()
    for i, line in enumerate(lines):
        for k, v in updates.items():
            if line.startswith(f"{k}="):
                lines[i] = f"{k}={v}"
                seen.add(k)
    for k, v in updates.items():
        if k not in seen:
            lines.append(f"{k}={v}")
    env_path.write_text("\n".join(lines) + "\n")
    for k, v in updates.items():
        os.environ[k] = v


@router.post("", response_model=AISettingsResponse)
async def apply_ai_settings(
    req: ApplyRequest,
    _admin: None = Depends(require_admin),
) -> AISettingsResponse:
    tier = _effective_tier()
    data = _load_candidates()
    candidates = data.get("tiers", {}).get(tier, {}).get("candidates", [])
    if not any(c["backend"] == req.backend and c["model"] == req.model for c in candidates):
        raise HTTPException(
            422,
            f"model not in candidates_for_tier for {tier}: {req.model}",
        )

    prev_backend = os.getenv("JARVIS_LLM_BACKEND")
    prev_model = os.getenv("JARVIS_SMART_MODEL")
    prev_profiles = os.getenv("COMPOSE_PROFILES", "")

    try:
        _patch_env_file(
            {
                "JARVIS_LLM_BACKEND": req.backend,
                "JARVIS_SMART_MODEL": req.model,
                "COMPOSE_PROFILES": "vllm" if req.backend == "vllm" else "",
            }
        )
        subprocess.run(
            ["bash", "scripts/render-litellm-config.sh"],
            env={
                **os.environ,
                "JARVIS_LLM_BACKEND": req.backend,
                "JARVIS_SMART_MODEL": req.model,
                "JARVIS_HW_TIER": tier,
            },
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["docker", "compose", "up", "-d"],
            check=True,
            capture_output=True,
        )
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                urllib.request.urlopen("http://localhost:8000/health/live", timeout=2).read()
                break
            except Exception:
                time.sleep(2)
        else:
            raise RuntimeError("backend /health/live did not become ready within 60s")
    except Exception as exc:
        _patch_env_file(
            {
                "JARVIS_LLM_BACKEND": prev_backend or "ollama",
                "JARVIS_SMART_MODEL": prev_model or "",
                "COMPOSE_PROFILES": prev_profiles,
            }
        )
        raise HTTPException(502, f"apply failed; reverted: {exc}") from exc

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
            json.dumps({"banner_kind": req.banner_kind}),
        )
    return {"ok": True}
