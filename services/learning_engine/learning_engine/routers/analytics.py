"""Analytics router - activity, reviews, retention, LLM cost."""

import asyncpg
from fastapi import APIRouter, Depends, Query, Request

from learning_engine.deps import get_db_pool, limiter
from learning_engine.models import ActivityItem, LLMCostItem, RetentionItem, ReviewDistributionItem

router = APIRouter(prefix="/api/analytics", tags=["analytics"])


# ---------------------------------------------------------------------------
# GET /api/analytics/activity
# ---------------------------------------------------------------------------


@router.get("/activity", response_model=list[ActivityItem])
@limiter.limit("60/minute")
async def get_activity(
    request: Request,
    days: int = Query(default=30, ge=1, le=365),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> list[ActivityItem]:
    """Return daily_log entries for the last N days."""
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT log_date, tasks_completed, cards_reviewed, papers_read, focus_hours, notes
            FROM daily_log
            WHERE log_date >= CURRENT_DATE - $1::int
            ORDER BY log_date ASC
            """,
            days,
        )
    return [ActivityItem(**dict(row)) for row in rows]


# ---------------------------------------------------------------------------
# GET /api/analytics/reviews
# ---------------------------------------------------------------------------


@router.get("/reviews", response_model=list[ReviewDistributionItem])
@limiter.limit("60/minute")
async def get_reviews(
    request: Request,
    days: int = Query(default=30, ge=1, le=365),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> list[ReviewDistributionItem]:
    """Return review rating distribution for the last N days."""
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT rating, COUNT(*) AS count
            FROM review_logs
            WHERE reviewed_at >= NOW() - make_interval(days => $1)
            GROUP BY rating
            ORDER BY rating
            """,
            days,
        )
    return [ReviewDistributionItem(**dict(row)) for row in rows]


# ---------------------------------------------------------------------------
# GET /api/analytics/retention
# ---------------------------------------------------------------------------


@router.get("/retention", response_model=list[RetentionItem])
@limiter.limit("60/minute")
async def get_retention(
    request: Request,
    days: int = Query(default=30, ge=1, le=365),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> list[RetentionItem]:
    """Return retention trend (Good+Easy % per day) for the last N days."""
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                (reviewed_at AT TIME ZONE 'UTC')::date AS review_date,
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE rating IN (3, 4)) AS good_easy,
                ROUND(
                    COUNT(*) FILTER (WHERE rating IN (3, 4))::numeric / NULLIF(COUNT(*), 0) * 100,
                    1
                ) AS retention_pct
            FROM review_logs
            WHERE reviewed_at >= NOW() - make_interval(days => $1)
            GROUP BY (reviewed_at AT TIME ZONE 'UTC')::date
            ORDER BY review_date
            """,
            days,
        )
    return [RetentionItem(**dict(row)) for row in rows]


# ---------------------------------------------------------------------------
# GET /api/analytics/llm-cost
# ---------------------------------------------------------------------------


@router.get("/llm-cost", response_model=list[LLMCostItem])
@limiter.limit("60/minute")
async def get_llm_cost(
    request: Request,
    days: int = Query(default=30, ge=1, le=365),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> list[LLMCostItem]:
    """Return daily LLM cost breakdown by workflow for the last N days."""
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                (created_at AT TIME ZONE 'UTC')::date AS day,
                SUM(cost_usd)::float AS total_cost,
                workflow
            FROM llm_usage_log
            WHERE created_at >= NOW() - make_interval(days => $1)
            GROUP BY (created_at AT TIME ZONE 'UTC')::date, workflow
            ORDER BY day
            """,
            days,
        )
    return [LLMCostItem(**dict(row)) for row in rows]
