"""Paper priority endpoints."""

import logging
from datetime import UTC, datetime

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request

from app.deps import get_db_pool, limiter
from app.models import (
    PaperPriorityResponse,
    RecomputePrioritiesResponse,
    compute_priority,
    priority_level,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["priority"])


@router.post("/api/papers/{paper_id}/priority", response_model=PaperPriorityResponse)
@limiter.limit("60/minute")
async def compute_paper_priority(
    request: Request,
    paper_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
):
    """Compute and store the priority score for a paper.

    Parameters
    ----------
    paper_id : int
        Database paper ID.

    Returns
    -------
    dict
        ``{paper_id, priority_score, priority_level}``
    """
    async with db_pool.acquire() as conn:
        paper = await conn.fetchrow(
            "SELECT id, discovered_at, citation_count FROM papers WHERE id = $1",
            paper_id,
        )
        if not paper:
            raise HTTPException(status_code=404, detail="Paper not found")

        rows = await conn.fetch(
            "SELECT relevance_score FROM paper_topics WHERE paper_id = $1 AND relevance_score IS NOT NULL",
            paper_id,
        )
        scores = [r["relevance_score"] for r in rows]

        now = datetime.now(UTC)
        score = compute_priority(scores, paper["discovered_at"], paper["citation_count"], now)

        await conn.execute(
            "UPDATE papers SET priority_score = $1 WHERE id = $2",
            score,
            paper_id,
        )

    level = priority_level(score)
    return {"paper_id": paper_id, "priority_score": score, "priority_level": level}


@router.post("/api/papers/recompute-priorities", response_model=RecomputePrioritiesResponse)
@limiter.limit("5/minute")
async def recompute_all_priorities(
    request: Request,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
):
    """Recompute priority scores for all papers.

    Returns
    -------
    dict
        ``{updated: int}``
    """
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT p.id, p.discovered_at, p.citation_count,
                   ARRAY_AGG(pt.relevance_score) FILTER (WHERE pt.relevance_score IS NOT NULL) as scores
            FROM papers p
            LEFT JOIN paper_topics pt ON p.id = pt.paper_id
            GROUP BY p.id
            """
        )

        now = datetime.now(UTC)
        updates: list[tuple[float, int]] = []
        for row in rows:
            scores = list(row["scores"]) if row["scores"] else []
            score = compute_priority(scores, row["discovered_at"], row["citation_count"], now)
            updates.append((score, row["id"]))

        if updates:
            await conn.executemany(
                "UPDATE papers SET priority_score = $1 WHERE id = $2",
                updates,
            )

    return {"updated": len(updates)}
