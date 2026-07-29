"""Job handlers for cross-paper contradiction scans."""

from __future__ import annotations

from typing import Any

import asyncpg
import httpx
from jarvis_common.jobs import ProgressContext

from paper_ingestion._state import get_services
from paper_ingestion.services.contradictions import scan_contradictions

__all__ = ["_contradictions_scan_job"]


async def _contradictions_scan_job(
    pool: asyncpg.Pool,
    http_client: httpx.AsyncClient,
    payload: dict[str, Any],
    ctx: ProgressContext,
) -> dict[str, Any]:
    """Scan verified paper findings for quote-verified contradictions."""
    services = get_services()
    verifier = services.verifier
    if verifier is None:
        raise RuntimeError("verifier not initialized")
    openai_client = services.openai_client
    if openai_client is None:
        raise RuntimeError("openai_client not initialized")
    paper_id = payload.get("paper_id")
    limit = int(payload.get("limit") or 25)
    user_id = int(payload["user_id"])
    await ctx.update_progress(0.1, "Collecting verified findings")
    result = await scan_contradictions(
        pool,
        http_client,
        verifier,
        openai_client=openai_client,
        paper_id=int(paper_id) if paper_id is not None else None,
        limit=limit,
        user_id=user_id,
    )
    await ctx.update_progress(1.0, "Done")
    return result
