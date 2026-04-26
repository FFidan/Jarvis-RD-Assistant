"""Job handler for extraction.batch.

Registered with the jarvis_common jobs backbone; imported at startup (see
main.py) so the ``@job_handler`` decorator populates ``_HANDLERS``.

Handler signature: async (pool, http_client, payload, ctx) -> dict
"""

from __future__ import annotations

import logging
from typing import Any

import asyncpg
import httpx
from jarvis_common.jobs import JobContext, job_handler

from paper_ingestion._state import svc

logger = logging.getLogger(__name__)

__all__ = [
    "_extraction_single_job",
    "_extraction_batch_job",
]


@job_handler("extraction.single")
async def _extraction_single_job(
    pool: asyncpg.Pool,
    http_client: httpx.AsyncClient,
    payload: dict[str, Any],
    ctx: JobContext,
) -> dict[str, Any]:
    """Extract structured fields for one paper/template pair."""
    from paper_ingestion.extraction import extract_fields_for_paper

    paper_id = int(payload["paper_id"])
    template_id = int(payload["template_id"])
    await ctx.update_progress(0.1, "Extracting fields")
    result = await extract_fields_for_paper(
        http_client,
        pool,
        paper_id,
        template_id,
        embedder=svc.embedder,
        verifier=svc.verifier,
    )
    await ctx.update_progress(1.0, "Done")
    return result.model_dump(mode="json")


@job_handler("extraction.batch")
async def _extraction_batch_job(
    pool: asyncpg.Pool,
    http_client: httpx.AsyncClient,
    payload: dict[str, Any],
    ctx: JobContext,
) -> dict[str, Any]:
    """Extract structured fields for a batch of papers.

    Payload keys:
        paper_ids (list[int]): DB paper IDs to extract.
        template_id (int): Extraction template to apply.
    """
    from paper_ingestion.extraction import batch_extract

    paper_ids: list[int] = list(payload.get("paper_ids", []))
    template_id: int = int(payload["template_id"])

    embedder = svc.embedder
    verifier = svc.verifier

    result = await batch_extract(
        http_client,
        pool,
        paper_ids,
        template_id,
        embedder=embedder,
        verifier=verifier,
        ctx=ctx,
    )
    return {
        "extracted": result.extracted,
        "failed": result.failed,
        "skipped": result.skipped,
        "total": len(paper_ids),
    }
