"""Citation graph endpoints.

Fetch citations from Semantic Scholar, build citation graphs,
and query stored citation relationships.
"""

import logging
import uuid
from typing import Annotated

import asyncpg
from fastapi import APIRouter, Depends, Query, Request
from jarvis_common.auth import current_user_id_strict
from jarvis_common.db_helpers import assert_paper_ownership
from jarvis_common.task_registry import KIND_TO_TASK

from paper_ingestion.citations import (
    _filter_visible_paper_ids,
    build_citation_graph,
    sync_citations_for_paper,
)
from paper_ingestion.deps import get_db_pool, get_s2_source, limiter
from paper_ingestion.models import (
    BatchCitationFetchResponse,
    CitationFetchResponse,
    CitationGraphResponse,
    CitationRelation,
)
from paper_ingestion.sources.semantic_scholar_source import SemanticScholarSource

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/citations", tags=["citations"])


@router.get("/graph", response_model=CitationGraphResponse)
@limiter.limit("60/minute")
async def get_citation_graph(
    request: Request,
    paper_ids: Annotated[list[int], Query(max_length=100)],
    depth: int = Query(default=1, ge=1, le=2),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> CitationGraphResponse:
    """Build a citation graph for the given paper IDs."""
    user_id = await current_user_id_strict(request)
    async with db_pool.acquire() as conn:
        # Strict-fail: raise 403/404 on first paper the caller does not own.
        # Per-id loop is O(n) DB round-trips but correct and simple (YAGNI).
        for pid in paper_ids:
            await assert_paper_ownership(conn, pid, user_id)
        return await build_citation_graph(conn, paper_ids, depth, user_id=user_id)


@router.post("/batch-fetch", response_model=BatchCitationFetchResponse)
@limiter.limit("2/minute")
async def batch_fetch_citations(
    request: Request,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> BatchCitationFetchResponse:
    """Enqueue a citations.batch_fetch job for papers without citations_fetched_at.

    The job is durable and visible in the jobs table.  The handler runs in the
    paper_ingestion worker loop (``citations_jobs.py``).
    """
    user_id = await current_user_id_strict(request)
    jarvis_job_id = str(uuid.uuid4())
    # Phase 2 WS-2D: pass caller user_id for audit-trail attribution. The job
    # body is still system-wide discovery (batched across all papers), but the
    # user that triggered the batch should be recorded.
    await KIND_TO_TASK["citations.batch_fetch"].defer_async(job_id=jarvis_job_id, user_id=user_id)
    return BatchCitationFetchResponse(queued=1, message=f"Job {jarvis_job_id} queued")


@router.post("/{paper_id}/fetch", response_model=CitationFetchResponse)
@limiter.limit("10/minute")
async def fetch_citations_for_paper(
    request: Request,
    paper_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    s2_source: SemanticScholarSource = Depends(get_s2_source),
) -> CitationFetchResponse:
    """Trigger citation fetch from S2 for a single paper."""
    user_id = await current_user_id_strict(request)
    async with db_pool.acquire() as conn:
        await assert_paper_ownership(conn, paper_id, user_id)
    # Pass pool so S2 API calls happen outside DB connection scope (PI-014)
    return await sync_citations_for_paper(db_pool, s2_source, paper_id)


@router.get("/{paper_id}", response_model=list[CitationRelation])
@limiter.limit("60/minute")
async def get_paper_citations(
    request: Request,
    paper_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> list[CitationRelation]:
    """Get stored citation relationships for a paper."""
    user_id = await current_user_id_strict(request)
    async with db_pool.acquire() as conn:
        await assert_paper_ownership(conn, paper_id, user_id)

        try:
            rows = await conn.fetch(
                """SELECT source_paper_id, cited_paper_id, citation_context,
                          is_influential, intent
                   FROM paper_citations
                   WHERE source_paper_id = $1 OR cited_paper_id = $1
                   ORDER BY fetched_at DESC""",
                paper_id,
            )
        except asyncpg.exceptions.UndefinedTableError:
            return []

        # W1-D2-003: drop rows whose counter-party paper is not visible to
        # the caller.  A single follow-up SELECT checks visibility in bulk.
        if rows:
            counter_party_ids = [
                r["cited_paper_id"] if r["source_paper_id"] == paper_id else r["source_paper_id"]
                for r in rows
            ]
            visible_ids = set(await _filter_visible_paper_ids(conn, counter_party_ids, user_id))
            # The seed paper itself is always visible (ownership was asserted above).
            visible_ids.add(paper_id)
            rows = [
                r
                for r in rows
                if r["cited_paper_id"] in visible_ids and r["source_paper_id"] in visible_ids
            ]

    return [
        CitationRelation(
            source_paper_id=r["source_paper_id"],
            cited_paper_id=r["cited_paper_id"],
            citation_context=r["citation_context"],
            is_influential=r["is_influential"],
            intent=r["intent"] or [],
        )
        for r in rows
    ]
