"""Job handler for citations.batch_fetch.

Procrastinate task wrappers for citation jobs; called via task_registry.py.

Handler signature: async (pool, http_client, payload, ctx) -> dict
"""

from __future__ import annotations

import logging
from typing import Any

import asyncpg
import httpx
from jarvis_common.jobs import ProgressContext

from paper_ingestion._state import get_services
from paper_ingestion.sources.semantic_scholar_source import SemanticScholarSource

logger = logging.getLogger(__name__)

__all__ = [
    "_citations_batch_fetch_job",
]


async def _citations_batch_fetch_job(
    pool: asyncpg.Pool,
    http_client: httpx.AsyncClient,
    payload: dict[str, Any],
    ctx: ProgressContext,
) -> dict[str, Any]:
    """Fetch citations from Semantic Scholar for papers that lack them.

    Payload keys:
        (none — fetches up to 50 un-fetched S2 papers automatically)
    """
    from paper_ingestion.citations import sync_citations_for_paper  # noqa: PLC0415

    sources = get_services().sources or {}
    s2_source: SemanticScholarSource | None = sources.get("semantic_scholar")
    if s2_source is None:
        raise RuntimeError("Semantic Scholar source not available")

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id FROM papers
               WHERE citations_fetched_at IS NULL
                 AND external_id LIKE 's2:%'
                 AND (metadata->>'stub' IS NULL OR metadata->>'stub' != 'true')
               ORDER BY created_at DESC
               LIMIT 50"""
        )
    paper_ids = [r["id"] for r in rows]

    if not paper_ids:
        return {"fetched": 0, "failed": 0, "message": "No papers need citation fetching"}

    fetched = 0
    failed = 0
    for i, pid in enumerate(paper_ids):
        try:
            await sync_citations_for_paper(pool, s2_source, pid)
            fetched += 1
        except Exception:
            logger.exception("Failed batch citation fetch for paper %d", pid)
            failed += 1
        await ctx.update_progress((i + 1) / len(paper_ids))

    return {
        "fetched": fetched,
        "failed": failed,
        "message": f"Fetched citations for {fetched}/{len(paper_ids)} papers",
    }
