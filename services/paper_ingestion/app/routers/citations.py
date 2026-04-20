"""Citation graph endpoints.

Fetch citations from Semantic Scholar, build citation graphs,
and query stored citation relationships.
"""

import logging
from typing import Annotated

import asyncpg
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from jarvis_common.auth import verify_api_key

from app.citations import build_citation_graph, sync_citations_for_paper
from app.deps import get_db_pool, limiter
from app.models import (
    BatchCitationFetchResponse,
    CitationFetchResponse,
    CitationGraphResponse,
    CitationRelation,
)
from app.sources.semantic_scholar_source import SemanticScholarSource

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/citations",
    tags=["citations"],
    dependencies=[Depends(verify_api_key)],
)


def _get_s2_source(request: Request) -> SemanticScholarSource:
    """Get S2 source from app state, or create a default one."""
    s2 = getattr(request.app.state, "s2_source", None)
    if s2 is not None:
        return s2
    # Fallback: check registered sources
    sources = getattr(request.app.state, "sources", {})
    s2 = sources.get("semantic_scholar")
    if s2 is not None:
        return s2
    raise HTTPException(503, "Semantic Scholar source not available")


@router.get("/graph", response_model=CitationGraphResponse)
@limiter.limit("60/minute")
async def get_citation_graph(
    request: Request,
    paper_ids: Annotated[list[int], Query(max_length=100)],
    depth: int = Query(default=1, ge=1, le=2),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> CitationGraphResponse:
    """Build a citation graph for the given paper IDs."""
    async with db_pool.acquire() as conn:
        return await build_citation_graph(conn, paper_ids, depth)


@router.post("/batch-fetch", response_model=BatchCitationFetchResponse)
@limiter.limit("2/minute")
async def batch_fetch_citations(
    request: Request,
    background_tasks: BackgroundTasks,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
):
    """Queue citation fetching for all papers without citations_fetched_at."""
    async with db_pool.acquire() as conn:
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
        return {"queued": 0, "message": "No papers need citation fetching"}

    # Capture references before creating the closure — after the response is
    # sent the request object becomes invalid (PI-003).
    s2_source = _get_s2_source(request)

    async def _fetch_batch(
        pool: asyncpg.Pool, source: SemanticScholarSource, pids: list[int]
    ) -> None:
        for pid in pids:
            try:
                await sync_citations_for_paper(pool, source, pid)
            except Exception:
                logger.exception("Failed batch citation fetch for paper %d", pid)

    background_tasks.add_task(_fetch_batch, db_pool, s2_source, paper_ids)
    return {"queued": len(paper_ids), "message": f"Fetching citations for {len(paper_ids)} papers"}


@router.post("/{paper_id}/fetch", response_model=CitationFetchResponse)
@limiter.limit("10/minute")
async def fetch_citations_for_paper(
    request: Request,
    paper_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> CitationFetchResponse:
    """Trigger citation fetch from S2 for a single paper."""
    s2_source = _get_s2_source(request)
    async with db_pool.acquire() as conn:
        exists = await conn.fetchval("SELECT id FROM papers WHERE id = $1", paper_id)
        if not exists:
            raise HTTPException(404, f"Paper {paper_id} not found")
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
    async with db_pool.acquire() as conn:
        exists = await conn.fetchval("SELECT id FROM papers WHERE id = $1", paper_id)
        if not exists:
            raise HTTPException(404, f"Paper {paper_id} not found")

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
