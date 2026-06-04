"""Job handler for extraction.batch.

Procrastinate task wrappers for extraction jobs; called via task_registry.py.

Handler signature: async (pool, http_client, payload, ctx) -> dict
"""

from __future__ import annotations

import logging
from typing import Any

import asyncpg
import httpx
from jarvis_common.jobs import ProgressContext

from paper_ingestion._state import get_services

logger = logging.getLogger(__name__)

__all__ = [
    "_extraction_single_job",
    "_extraction_batch_job",
]


async def _extraction_single_job(
    pool: asyncpg.Pool,
    http_client: httpx.AsyncClient,
    payload: dict[str, Any],
    ctx: ProgressContext,
) -> dict[str, Any]:
    """Extract structured fields for one paper/template pair."""
    from jarvis_common.db_helpers import assert_paper_ownership  # noqa: PLC0415

    from paper_ingestion.extraction import extract_fields_for_paper  # noqa: PLC0415

    paper_id = int(payload["paper_id"])
    template_id = int(payload["template_id"])
    user_id = payload.get("user_id")

    # Re-validate ownership at job execution time to prevent IDOR via queued jobs.
    async with pool.acquire() as conn:
        await assert_paper_ownership(conn, paper_id, user_id)

    await ctx.update_progress(0.1, "Extracting fields")
    services = get_services()
    result = await extract_fields_for_paper(
        http_client,
        pool,
        paper_id,
        template_id,
        embedder=services.embedder,
        verifier=services.verifier,
        user_id=user_id,
    )
    await ctx.update_progress(1.0, "Done")
    return result.model_dump(mode="json")


async def _extraction_batch_job(
    pool: asyncpg.Pool,
    http_client: httpx.AsyncClient,
    payload: dict[str, Any],
    ctx: ProgressContext,
) -> dict[str, Any]:
    """Extract structured fields for a batch of papers.

    Payload keys:
        paper_ids (list[int]): DB paper IDs to extract.
        template_id (int): Extraction template to apply.
        user_id (int | None): Caller user ID for ownership check.
    """
    from jarvis_common.db_helpers import assert_paper_ownership  # noqa: PLC0415

    from paper_ingestion.extraction import batch_extract  # noqa: PLC0415

    paper_ids: list[int] = list(payload.get("paper_ids", []))
    template_id: int = int(payload["template_id"])
    user_id = payload.get("user_id")

    # Re-validate ownership for each paper at job execution time.
    async with pool.acquire() as conn:
        for paper_id in paper_ids:
            await assert_paper_ownership(conn, paper_id, user_id)

    services = get_services()
    embedder = services.embedder
    verifier = services.verifier

    result = await batch_extract(
        http_client,
        pool,
        paper_ids,
        template_id,
        embedder=embedder,
        verifier=verifier,
        ctx=ctx,
        user_id=user_id,
    )
    return {
        "extracted": result.extracted,
        "failed": result.failed,
        "skipped": result.skipped,
        "total": len(paper_ids),
    }
