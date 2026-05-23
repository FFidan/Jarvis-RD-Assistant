"""Analytics router - activity, reviews, retention, LLM cost, summary."""

import datetime

import asyncpg
from fastapi import APIRouter, Depends, Query, Request
from jarvis_common.auth import current_user_id_strict

from learning_engine.deps import get_db_pool, limiter
from learning_engine.models import (
    ActivityItem,
    AnalyticsSummaryResponse,
    LLMCostItem,
    RetentionItem,
    ReviewDistributionItem,
)

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
    user_id: int = Depends(current_user_id_strict),
) -> list[ActivityItem]:
    """Return daily_log entries for the last N days, scoped to the calling user.

    Excludes today for stable KPI snapshot; mirrors /summary semantic.
    """
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT log_date, tasks_completed, cards_reviewed, papers_read, focus_hours, notes
            FROM daily_log
            WHERE user_id = $1
              AND log_date >= CURRENT_DATE - $2::int
              AND log_date <  CURRENT_DATE
            ORDER BY log_date ASC
            """,
            user_id,
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
    user_id: int = Depends(current_user_id_strict),
) -> list[ReviewDistributionItem]:
    """Return review rating distribution for the last N days, scoped to the calling user."""
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT rating, COUNT(*) AS count
            FROM review_logs
            WHERE user_id = $1
              AND reviewed_at >= NOW() - make_interval(days => $2)
            GROUP BY rating
            ORDER BY rating
            """,
            user_id,
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
    user_id: int = Depends(current_user_id_strict),
) -> list[RetentionItem]:
    """Return retention trend (Good+Easy % per day) for the last N days, scoped to caller."""
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
            WHERE user_id = $1
              AND reviewed_at >= NOW() - make_interval(days => $2)
            GROUP BY (reviewed_at AT TIME ZONE 'UTC')::date
            ORDER BY review_date
            """,
            user_id,
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
    user_id: int = Depends(current_user_id_strict),
) -> list[LLMCostItem]:
    """Return daily LLM cost breakdown by workflow for the last N days, scoped to caller."""
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                (created_at AT TIME ZONE 'UTC')::date AS day,
                SUM(cost_usd)::float AS total_cost,
                workflow
            FROM llm_usage_log
            WHERE user_id = $1
              AND created_at >= NOW() - make_interval(days => $2)
            GROUP BY (created_at AT TIME ZONE 'UTC')::date, workflow
            ORDER BY day
            """,
            user_id,
            days,
        )
    return [LLMCostItem(**dict(row)) for row in rows]


# ---------------------------------------------------------------------------
# Shared helper
# ---------------------------------------------------------------------------


def _compute_streak(rows: list, *, field: str) -> int:
    """Count consecutive days (descending from today/yesterday) where *field* > 0.

    Mirrors the focus-streak algorithm in executive.py lines 99-119 exactly:
    - Fetch rows already filtered to field > 0, ordered by log_date DESC.
    - Allow the streak to begin on today or yesterday (handles the common case
      where the user hasn't yet logged today).
    - Walks rows forward in time (reversed desc), checking each expected date.
    """
    if not rows:
        return 0
    today = datetime.datetime.now(datetime.UTC).date()
    expected = (
        rows[0]["log_date"] if rows[0]["log_date"] == today else today - datetime.timedelta(days=1)
    )
    if rows[0]["log_date"] != expected:
        return 0
    streak = 0
    for row in rows:
        if row["log_date"] == expected:
            streak += 1
            expected -= datetime.timedelta(days=1)
        else:
            break
    return streak


# ---------------------------------------------------------------------------
# GET /api/analytics/summary
# ---------------------------------------------------------------------------


@router.get("/summary", response_model=AnalyticsSummaryResponse)
@limiter.limit("60/minute")
async def get_analytics_summary(
    request: Request,
    days: int = Query(default=30, ge=1, le=365),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(current_user_id_strict),
) -> AnalyticsSummaryResponse:
    """Return KPI summary: current/prior-period totals and streaks, scoped to caller.

    Current period:  [today - days,   today)
    Previous period: [today - 2*days, today - days)
    Both windows use CURRENT_DATE arithmetic so month boundaries are handled
    correctly by PostgreSQL date subtraction.
    """
    async with db_pool.acquire() as conn:
        # --- Current-period totals ---
        current_row = await conn.fetchrow(
            """
            SELECT
                COALESCE(SUM(papers_read), 0)::int     AS papers_read_total,
                COALESCE(SUM(focus_hours), 0.0)::float AS focus_hours_total,
                COALESCE(SUM(cards_reviewed), 0)::int  AS cards_reviewed_total
            FROM daily_log
            WHERE user_id = $1
              AND log_date >= CURRENT_DATE - $2::int
              AND log_date <  CURRENT_DATE
            """,
            user_id,
            days,
        )

        # --- Prior-period totals ---
        prev_row = await conn.fetchrow(
            """
            SELECT
                COALESCE(SUM(papers_read), 0)::int     AS papers_read_prev,
                COALESCE(SUM(focus_hours), 0.0)::float AS focus_hours_prev,
                COALESCE(SUM(cards_reviewed), 0)::int  AS cards_reviewed_prev
            FROM daily_log
            WHERE user_id = $1
              AND log_date >= CURRENT_DATE - ($2::int * 2)
              AND log_date <  CURRENT_DATE - $2::int
            """,
            user_id,
            days,
        )

        # --- Focus streak: consecutive days with focus_hours > 0 ---
        focus_rows = await conn.fetch(
            "SELECT log_date FROM daily_log "
            "WHERE focus_hours > 0 AND user_id = $1 "
            "ORDER BY log_date DESC LIMIT 365",
            user_id,
        )

        # --- Cards-review streak: consecutive days with cards_reviewed > 0 ---
        review_rows = await conn.fetch(
            "SELECT log_date FROM daily_log "
            "WHERE cards_reviewed > 0 AND user_id = $1 "
            "ORDER BY log_date DESC LIMIT 365",
            user_id,
        )

    return AnalyticsSummaryResponse(
        papers_read_total=current_row["papers_read_total"],
        focus_hours_total=current_row["focus_hours_total"],
        cards_reviewed_total=current_row["cards_reviewed_total"],
        papers_read_prev=prev_row["papers_read_prev"],
        focus_hours_prev=prev_row["focus_hours_prev"],
        cards_reviewed_prev=prev_row["cards_reviewed_prev"],
        focus_streak_days=_compute_streak(focus_rows, field="focus_hours"),
        cards_review_streak_days=_compute_streak(review_rows, field="cards_reviewed"),
    )
