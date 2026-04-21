"""Review and stats endpoints."""

from datetime import UTC, date, datetime, timedelta

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from jarvis_common import ErrorResponse

from learning_engine.converters import row_to_card_response
from learning_engine.deps import get_db_pool, get_fsrs_manager, limiter
from learning_engine.fsrs_manager import FSRSManager
from learning_engine.models import CardResponse, RetentionStats, ReviewRequest, ReviewResponse

router = APIRouter(
    prefix="/api",
    tags=["review"],
    responses={
        400: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
)


@router.get("/review/next", response_model=list[CardResponse])
@limiter.limit("60/minute")
async def get_next_review(
    request: Request,
    limit: int = Query(default=1, ge=1, le=50),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> list[CardResponse]:
    """Get next due card(s) for review."""
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM cards WHERE due_at <= NOW() ORDER BY due_at ASC LIMIT $1",
            limit,
        )
    return [row_to_card_response(row) for row in rows]


@router.post("/review/{card_id}", response_model=ReviewResponse)
@limiter.limit("60/minute")
async def submit_review(
    request: Request,
    card_id: int,
    body: ReviewRequest,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    fsrs_manager: FSRSManager = Depends(get_fsrs_manager),
) -> ReviewResponse:
    """Submit a review for a card. Atomic: updates FSRS state and logs review."""
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow("SELECT * FROM cards WHERE id = $1 FOR UPDATE", card_id)
            if not row:
                raise HTTPException(status_code=404, detail="Card not found")

            new_state, log_dict, next_due = fsrs_manager.schedule_review(
                row["fsrs_state"], body.rating.value
            )

            await conn.execute(
                "UPDATE cards SET fsrs_state = $1, due_at = $2, updated_at = NOW() WHERE id = $3",
                new_state,
                next_due,
                card_id,
            )

            log_id = await conn.fetchval(
                """
                INSERT INTO review_logs (card_id, rating, review_duration_ms, fsrs_log)
                VALUES ($1, $2, $3, $4)
                RETURNING id
                """,
                card_id,
                body.rating.value,
                body.review_duration_ms,
                log_dict,
            )

    return ReviewResponse(
        card_id=card_id,
        rating=body.rating.value,
        next_due_at=next_due,
        fsrs_state=new_state,
        review_log_id=log_id,
    )


@router.get("/stats", response_model=RetentionStats)
@limiter.limit("60/minute")
async def get_stats(
    request: Request,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> RetentionStats:
    """Get retention and review statistics."""
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            # Single CTE query for card counts and rating breakdown
            stats_row = await conn.fetchrow(
                """
                WITH card_stats AS (
                    SELECT
                        COUNT(*) AS total_cards,
                        COUNT(*) FILTER (WHERE due_at <= NOW()) AS due_now
                    FROM cards
                ),
                today_stats AS (
                    SELECT COUNT(*) AS reviewed_today
                    FROM review_logs
                    WHERE (reviewed_at AT TIME ZONE 'UTC')::date = (NOW() AT TIME ZONE 'UTC')::date
                ),
                rating_stats AS (
                    SELECT rating, COUNT(*) AS cnt
                    FROM review_logs
                    WHERE reviewed_at >= NOW() - INTERVAL '30 days'
                    GROUP BY rating
                ),
                rating_agg AS (
                    SELECT
                        COALESCE(jsonb_object_agg(rating::text, cnt), '{}'::jsonb) AS by_rating,
                        COALESCE(SUM(cnt), 0) AS total_recent,
                        COALESCE(SUM(cnt) FILTER (WHERE rating IN (3, 4)), 0) AS good_easy
                    FROM rating_stats
                )
                SELECT
                    cs.total_cards,
                    cs.due_now,
                    ts.reviewed_today,
                    ra.by_rating,
                    ra.total_recent,
                    ra.good_easy
                FROM card_stats cs, today_stats ts, rating_agg ra
                """
            )

            total_cards = stats_row["total_cards"] or 0
            due_now = stats_row["due_now"] or 0
            reviewed_today = stats_row["reviewed_today"] or 0
            reviews_by_rating = dict(stats_row["by_rating"] or {})
            total_recent = stats_row["total_recent"]
            good_easy = stats_row["good_easy"]
            average_retention = (good_easy / total_recent * 100) if total_recent > 0 else 0.0

            # Streak: consecutive days with at least one review, backwards from today (UTC)
            streak_rows = await conn.fetch(
                """
                SELECT DISTINCT (reviewed_at AT TIME ZONE 'UTC')::date AS review_date
                FROM review_logs ORDER BY review_date DESC LIMIT 365
                """
            )
            streak_days = 0
            today = datetime.now(UTC).date()
            yesterday = today - timedelta(days=1)
            expected: date | None = None
            for row in streak_rows:
                rd = row["review_date"]
                if expected is None:
                    # Accept today or yesterday as streak start
                    if rd == today:
                        expected = today - timedelta(days=1)
                    elif rd == yesterday:
                        expected = yesterday - timedelta(days=1)
                    else:
                        break  # streak is already broken (gap > 1 day)
                    streak_days += 1
                elif rd == expected:
                    streak_days += 1
                    expected -= timedelta(days=1)
                else:
                    break

    return RetentionStats(
        total_cards=total_cards,
        due_now=due_now,
        reviewed_today=reviewed_today,
        average_retention=round(average_retention, 1),
        reviews_by_rating=reviews_by_rating,
        streak_days=streak_days,
    )
