"""Admin-only AI backend diagnostics endpoints.

Reports the hardware tier, observed backend traffic, and per-tier candidate
recommendations. Model assignment itself is handled by the Quick/Main/Embedding
role cards (the DB/LiteLLM control plane); this surface is read-only diagnostics
plus the hardware-change banner dismissal.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Depends
from jarvis_common.auth import require_admin
from jarvis_common.hw_detect import detect_tier
from jarvis_common.litellm_observer import observed_share
from pydantic import BaseModel, Field

from paper_ingestion.deps import get_db_pool
from paper_ingestion.services.ai_settings import (
    find_candidate_config_path,
    resolve_candidates_for_tier,
)

router = APIRouter(prefix="/api/settings/ai", tags=["settings"])

# Allow the session-based admin dependency without requiring X-API-Key.
# Mark as session-exempt so main.py can register with dependencies=[].
router.auth_exempt = True  # type: ignore[attr-defined]

_CONFIG_PATH = find_candidate_config_path()


class AISettingsResponse(BaseModel):
    hw_tier: str
    recommended_backend: str
    recommended_model: str
    observed_backend: str | None
    observed_recent_share: float
    candidates_for_tier: list[dict[str, Any]]
    candidate_issues: list[str] = Field(default_factory=list)
    eval_report_date: str | None


class DismissRequest(BaseModel):
    banner_kind: str = Field(max_length=64)


def _effective_tier() -> str:
    """Return JARVIS_HW_TIER env override if set, else live detect_tier()."""
    return os.getenv("JARVIS_HW_TIER") or detect_tier()


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
        observed_backend=served,
        observed_recent_share=share,
        candidates_for_tier=selection.candidates,
        candidate_issues=selection.issues,
        eval_report_date=selection.generated_at,
    )


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
