"""Review and stats endpoints."""

import json
import logging
from datetime import datetime, timedelta

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from jarvis_common import ErrorResponse
from jarvis_common.auth import current_user_id_strict_with_owner_override
from jarvis_common.streak import compute_streak

from learning_engine.converters import row_to_card_response
from learning_engine.deps import get_db_pool, limiter
from learning_engine.fsrs_manager import FSRSManager
from learning_engine.models import (
    CardResponse,
    RetentionStats,
    ReviewRequest,
    ReviewResponse,
    ReviewSyncRequest,
    ReviewSyncResponse,
)

logger = logging.getLogger(__name__)


async def _build_fsrs_manager_from_db(
    conn: asyncpg.pool.PoolConnectionProxy,
    user_id: int | None = None,
) -> FSRSManager:
    """Read live fsrs.desired_retention and fsrs.learning_steps from user_config.

    When *user_id* is provided, user-specific rows take precedence over the
    NULL-row system defaults (DISTINCT ON keeps the user row first).
    Both keys are read per-review so that live edits take effect immediately
    without a service restart.

    When ``user_id`` is provided, per-user rows take precedence over the
    NULL-row system defaults (tried first; falls back to NULL row if absent).
    """
    desired_retention = 0.9
    learning_steps: list[timedelta] | None = None

    if user_id is not None:
        rows = await conn.fetch(
            """
            SELECT DISTINCT ON (key) key, value
              FROM user_config
             WHERE key IN ($1, $2)
               AND (user_id = $3 OR user_id IS NULL)
             ORDER BY key, (user_id IS NULL) ASC
            """,
            "fsrs.desired_retention",
            "fsrs.learning_steps",
            user_id,
        )
    else:
        rows = await conn.fetch(
            "SELECT key, value FROM user_config WHERE key IN ($1, $2) AND user_id IS NULL",
            "fsrs.desired_retention",
            "fsrs.learning_steps",
        )
    for row in rows:
        try:
            if row["key"] == "fsrs.desired_retention":
                desired_retention = float(row["value"])
            elif row["key"] == "fsrs.learning_steps":
                steps_raw = (
                    json.loads(row["value"]) if isinstance(row["value"], str) else row["value"]
                )
                if isinstance(steps_raw, list) and len(steps_raw) >= 1:
                    if len(steps_raw) < 2:
                        logger.warning(
                            "fsrs.learning_steps has %d element(s); FSRS default is 2 steps",
                            len(steps_raw),
                        )
                    learning_steps = [timedelta(minutes=int(s)) for s in steps_raw]
        except (ValueError, json.JSONDecodeError, TypeError):
            logger.warning(
                "Could not parse fsrs config key %s, using default", row["key"], exc_info=True
            )

    return FSRSManager(desired_retention=desired_retention, learning_steps=learning_steps)


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
    deck_id: int | None = Query(default=None, ge=1),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(current_user_id_strict_with_owner_override),
) -> list[CardResponse]:
    """Get next due card(s) for review, optionally scoped to a deck."""
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT c.* FROM cards c "
            "WHERE c.due_at <= NOW() "
            "AND c.user_id = $1 "
            "AND ($2::int IS NULL OR (c.deck_id = $2::int AND EXISTS ("
            "  SELECT 1 FROM decks d WHERE d.id = $2::int AND d.user_id = $1"
            "))) "
            "ORDER BY c.due_at ASC LIMIT $3",
            user_id,
            deck_id,
            limit,
        )
    return [row_to_card_response(row) for row in rows]


@router.post("/review/{card_id:int}", response_model=ReviewResponse)
@limiter.limit("60/minute")
async def submit_review(
    request: Request,
    card_id: int,
    body: ReviewRequest,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(current_user_id_strict_with_owner_override),
) -> ReviewResponse:
    """Submit a review for a card. Atomic: updates FSRS state and logs review.

    Builds a fresh FSRSManager per request so that live edits to
    fsrs.desired_retention and fsrs.learning_steps take effect immediately.
    """
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT * FROM cards WHERE id = $1 AND user_id = $2 FOR UPDATE",
                card_id,
                user_id,
            )
            if not row:
                raise HTTPException(status_code=404, detail="Card not found")

            fsrs_manager = await _build_fsrs_manager_from_db(conn, user_id=user_id)
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
                INSERT INTO review_logs (card_id, rating, review_duration_ms, fsrs_log, user_id)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id
                """,
                card_id,
                body.rating.value,
                body.review_duration_ms,
                log_dict,
                user_id,
            )

    return ReviewResponse(
        card_id=card_id,
        rating=body.rating.value,
        next_due_at=next_due,
        fsrs_state=new_state,
        review_log_id=log_id,
    )


@router.post("/review/sync", response_model=ReviewSyncResponse)
@limiter.limit("30/minute")
async def sync_reviews(
    request: Request,
    body: ReviewSyncRequest,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(current_user_id_strict_with_owner_override),
) -> ReviewSyncResponse:
    """Idempotently replay an offline review batch (contract 2026-05-16)."""
    synced = 0
    skipped = 0
    already_synced = 0
    if not body.reviews:
        return ReviewSyncResponse(synced=0, skipped=0, already_synced=0)

    # Pre-flight: resolve already-applied idempotency keys, then release connection.
    keys = [e.idempotency_key for e in body.reviews]
    async with db_pool.acquire() as conn:
        applied_rows = await conn.fetch(
            "SELECT idempotency_key FROM review_logs "
            "WHERE user_id = $1 AND idempotency_key = ANY($2::text[])",
            user_id,
            keys,
        )
        applied = {r["idempotency_key"] for r in applied_rows}
        fsrs_manager = await _build_fsrs_manager_from_db(conn, user_id=user_id)

    # Process events in chunks of 50; acquire a fresh connection per chunk.
    _batch_size = 50
    events = body.reviews
    for batch_start in range(0, len(events), _batch_size):
        batch = events[batch_start : batch_start + _batch_size]
        async with db_pool.acquire() as conn:
            for event in batch:
                if event.idempotency_key in applied:
                    already_synced += 1
                    continue
                async with conn.transaction():
                    card = await conn.fetchrow(
                        "SELECT * FROM cards WHERE id = $1 AND user_id = $2 FOR UPDATE",
                        event.card_id,
                        user_id,
                    )
                    if not card:
                        skipped += 1
                        continue
                    new_state, log_dict, next_due = fsrs_manager.schedule_review(
                        card["fsrs_state"], event.rating.value
                    )
                    inserted_log_id = await conn.fetchval(
                        """
                        INSERT INTO review_logs
                            (card_id, rating, review_duration_ms, reviewed_at,
                             fsrs_log, user_id, idempotency_key)
                        VALUES ($1, $2, $3, $4, $5, $6, $7)
                        ON CONFLICT (user_id, idempotency_key)
                            WHERE idempotency_key IS NOT NULL DO NOTHING
                        RETURNING id
                        """,
                        event.card_id,
                        event.rating.value,
                        event.review_duration_ms,
                        event.reviewed_at,
                        log_dict,
                        user_id,
                        event.idempotency_key,
                    )
                    if inserted_log_id is None:
                        # A concurrent request with the same idempotency_key won the
                        # INSERT (its row is durably recorded). Do NOT advance FSRS
                        # again — count as already_synced (not a new write) and move on.
                        applied.add(event.idempotency_key)
                        already_synced += 1
                        continue
                    await conn.execute(
                        "UPDATE cards SET fsrs_state = $1, due_at = $2, updated_at = NOW() "
                        "WHERE id = $3",
                        new_state,
                        next_due,
                        event.card_id,
                    )
                applied.add(event.idempotency_key)
                synced += 1

    return ReviewSyncResponse(synced=synced, skipped=skipped, already_synced=already_synced)


@router.get("/stats", response_model=RetentionStats)
@limiter.limit("60/minute")
async def get_stats(
    request: Request,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(current_user_id_strict_with_owner_override),
) -> RetentionStats:
    """Get retention and review statistics."""
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            stats_row = await conn.fetchrow(
                """
                WITH card_stats AS (
                    SELECT
                        COUNT(*) AS total_cards,
                        COUNT(*) FILTER (WHERE due_at <= NOW()) AS due_now
                    FROM cards
                    WHERE user_id IS NOT DISTINCT FROM $1
                ),
                today_stats AS (
                    SELECT COUNT(*) AS reviewed_today
                    FROM review_logs
                    WHERE (reviewed_at AT TIME ZONE 'UTC')::date = (NOW() AT TIME ZONE 'UTC')::date
                      AND user_id IS NOT DISTINCT FROM $1
                ),
                rating_stats AS (
                    SELECT rating, COUNT(*) AS cnt
                    FROM review_logs
                    WHERE reviewed_at >= NOW() - INTERVAL '30 days'
                      AND user_id IS NOT DISTINCT FROM $1
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
                """,
                user_id,
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
                FROM review_logs
                WHERE user_id = $1
                ORDER BY review_date DESC LIMIT 365
                """,
                user_id,
            )
            streak_days = compute_streak(
                [
                    datetime(r["review_date"].year, r["review_date"].month, r["review_date"].day)
                    for r in streak_rows
                ]
            )

    return RetentionStats(
        total_cards=total_cards,
        due_now=due_now,
        reviewed_today=reviewed_today,
        average_retention=round(average_retention, 1),
        reviews_by_rating=reviews_by_rating,
        streak_days=streak_days,
    )
