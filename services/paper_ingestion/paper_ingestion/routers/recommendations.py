"""Recommendation endpoints."""

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from paper_ingestion.deps import get_db_pool, limiter
from paper_ingestion.recommender import refresh_recommendations

router = APIRouter(prefix="/api/recommendations", tags=["recommendations"])


class RecommendationItem(BaseModel):
    paper_id: int
    score: float
    modes: list[str]
    explanation: str
    dismissed: bool


@router.get("", response_model=list[RecommendationItem])
@limiter.limit("5/minute")
async def list_recommendations(
    request: Request,
    limit: int = Query(default=20, ge=1, le=200),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> list[RecommendationItem]:
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT paper_id, score, modes, explanation, dismissed "
            "FROM paper_recommendations WHERE dismissed = FALSE "
            "ORDER BY score DESC LIMIT $1",
            limit,
        )
    return [RecommendationItem(**dict(r)) for r in rows]


@router.post("/refresh")
@limiter.limit("2/hour")
async def trigger_refresh(request: Request) -> dict[str, int]:
    count = await refresh_recommendations(request.app)
    return {"refreshed": count}


@router.post("/{paper_id}/dismiss")
@limiter.limit("30/minute")
async def dismiss_recommendation(
    paper_id: int,
    request: Request,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> dict[str, bool]:
    async with db_pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE paper_recommendations SET dismissed = TRUE WHERE paper_id = $1",
            paper_id,
        )
    if result == "UPDATE 0":
        raise HTTPException(status_code=404, detail="Recommendation not found")
    return {"dismissed": True}
