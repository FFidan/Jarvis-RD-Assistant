"""My Day journal endpoints.

Provides GET and POST (upsert) for end-of-day journal entries stored in the
``journal_entries`` table (migration 051).

Note: ``from __future__ import annotations`` is intentionally absent — see
``docs/plans/2026-04-29-future-import-failure-analysis.md`` for the verified
PydanticUserError trace.  Body annotations on Pydantic models must remain as
concrete types.
"""

import logging
from datetime import UTC, date, datetime, timedelta

import asyncpg
from fastapi import APIRouter, Depends, Query, Request
from jarvis_common.auth import current_user_id_strict

from paper_ingestion.deps import get_db_pool, limiter
from paper_ingestion.models.journal import (
    JournalEntryCreate,
    JournalEntryResponse,
    JournalPrompts,
)
from paper_ingestion.models.my_day import YesterdaySummary, YesterdayTask

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/my-day", tags=["my-day"])


# ---------------------------------------------------------------------------
# GET /api/my-day/journal?date=YYYY-MM-DD
# ---------------------------------------------------------------------------


@router.get("/journal", response_model=JournalEntryResponse | None)
@limiter.limit("60/minute")
async def get_journal_entry(
    request: Request,
    date: date = Query(..., description="ISO date YYYY-MM-DD"),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> JournalEntryResponse | None:
    """Fetch a journal entry for the given date.

    Returns ``None`` (HTTP 200 + JSON ``null``) when the user has no entry for
    that date — an empty state, not an error, so the dashboard does not log a
    console 404 for days the user has not journaled. The query is scoped to the
    caller's ``user_id``, so a non-owner simply sees no row (same empty state).
    """
    user_id = await current_user_id_strict(request)
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, date, prompts, created_at, updated_at "
            "FROM journal_entries "
            "WHERE user_id = $1 AND date = $2",
            user_id,
            date,
        )
    if row is None:
        return None
    return JournalEntryResponse(
        id=row["id"],
        date=row["date"],
        prompts=JournalPrompts(**(row["prompts"] or {})),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# ---------------------------------------------------------------------------
# POST /api/my-day/journal  (upsert — one entry per user per date)
# ---------------------------------------------------------------------------


@router.post("/journal", response_model=JournalEntryResponse)
@limiter.limit("60/minute")
async def upsert_journal_entry(
    request: Request,
    body: JournalEntryCreate,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> JournalEntryResponse:
    """Create or update a journal entry for the given date."""
    user_id = await current_user_id_strict(request)
    prompts_dict = body.prompts.model_dump(exclude_none=True)
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO journal_entries (user_id, date, prompts, updated_at)
            VALUES ($1, $2, $3::jsonb, NOW())
            ON CONFLICT ON CONSTRAINT journal_entries_user_id_date_key
            DO UPDATE SET prompts = EXCLUDED.prompts, updated_at = NOW()
            RETURNING id, date, prompts, created_at, updated_at
            """,
            user_id,
            body.date,
            prompts_dict,
        )
    return JournalEntryResponse(
        id=row["id"],
        date=row["date"],
        prompts=JournalPrompts(**(row["prompts"] or {})),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# ---------------------------------------------------------------------------
# GET /api/my-day/yesterday  (on-the-fly rollup — NO materialized job)
# ---------------------------------------------------------------------------
#
# Spec §3.2 / §4.2: § Yesterday is derived live from existing tables, removing
# the stated daily-rollup-job blocker. ``tz_offset_minutes`` is the caller's UTC
# offset in minutes (minutes EAST of UTC, i.e. JS ``-getTimezoneOffset()``); the
# server stores no per-user timezone so the client supplies it. Default 0 = UTC.
#
# "Yesterday" = the calendar day before the caller's *current local* day. Its
# UTC window is [local_midnight_yesterday, local_midnight_today) shifted back by
# the offset. ``tasks.completed_at`` / ``daily_log.log_date`` are matched to it.


@router.get("/yesterday", response_model=YesterdaySummary)
@limiter.limit("60/minute")
async def get_yesterday(
    request: Request,
    tz_offset_minutes: int = Query(
        0,
        ge=-840,
        le=840,
        description="Caller UTC offset in minutes east of UTC (JS -getTimezoneOffset()).",
    ),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> YesterdaySummary:
    """On-the-fly § Yesterday rollup for the authenticated caller only."""
    user_id = await current_user_id_strict(request)

    offset = timedelta(minutes=tz_offset_minutes)
    now_local = datetime.now(UTC) + offset
    yesterday_local_date = (now_local - timedelta(days=1)).date()
    # Local-midnight boundaries re-expressed as UTC instants.
    start_utc = (
        datetime(
            yesterday_local_date.year,
            yesterday_local_date.month,
            yesterday_local_date.day,
            tzinfo=UTC,
        )
        - offset
    )
    end_utc = start_utc + timedelta(days=1)

    async with db_pool.acquire() as conn:
        completed_rows = await conn.fetch(
            "SELECT id, title, status FROM tasks "
            "WHERE user_id = $1 AND status = 'done' "
            "AND completed_at >= $2 AND completed_at < $3 "
            "ORDER BY completed_at",
            user_id,
            start_utc,
            end_utc,
        )
        # Deferred = still-open work that was due during yesterday's window but
        # not completed — these get the "carry over →" affordance.
        deferred_rows = await conn.fetch(
            "SELECT id, title, status FROM tasks "
            "WHERE user_id = $1 AND status IN ('todo', 'in_progress', 'blocked') "
            "AND (deadline IS NOT NULL AND deadline >= $2 AND deadline < $3) "
            "ORDER BY deadline",
            user_id,
            start_utc,
            end_utc,
        )
        log_row = await conn.fetchrow(
            "SELECT focus_hours, cards_reviewed FROM daily_log "
            "WHERE user_id = $1 AND log_date = $2",
            user_id,
            yesterday_local_date,
        )

    focused_hours = float(log_row["focus_hours"]) if log_row else 0.0
    cards_reviewed = int(log_row["cards_reviewed"]) if log_row else 0

    return YesterdaySummary(
        date=yesterday_local_date,
        focused_hours=focused_hours,
        cards_reviewed=cards_reviewed,
        tasks_done=len(completed_rows),
        completed=[
            YesterdayTask(id=r["id"], title=r["title"], status=r["status"]) for r in completed_rows
        ],
        deferred=[
            YesterdayTask(id=r["id"], title=r["title"], status=r["status"]) for r in deferred_rows
        ],
    )
