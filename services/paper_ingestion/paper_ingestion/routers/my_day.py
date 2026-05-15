"""My Day journal endpoints (Phase 2).

Provides GET and POST (upsert) for end-of-day journal entries stored in the
``journal_entries`` table (migration 051).

Note: ``from __future__ import annotations`` is intentionally absent — see
``docs/plans/2026-04-29-future-import-failure-analysis.md`` for the verified
PydanticUserError trace.  Body annotations on Pydantic models must remain as
concrete types.
"""

import logging

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from jarvis_common.auth import current_user_id_strict

from paper_ingestion.deps import get_db_pool, limiter
from paper_ingestion.models.journal import (
    JournalEntryCreate,
    JournalEntryResponse,
    JournalPrompts,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/my-day", tags=["my-day"])


# ---------------------------------------------------------------------------
# GET /api/my-day/journal?date=YYYY-MM-DD
# ---------------------------------------------------------------------------


@router.get("/journal", response_model=JournalEntryResponse)
@limiter.limit("60/minute")
async def get_journal_entry(
    request: Request,
    date: str = Query(..., description="ISO date YYYY-MM-DD"),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> JournalEntryResponse:
    """Fetch a journal entry for the given date (404 if not found)."""
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
        raise HTTPException(status_code=404, detail="Journal entry not found")
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
            str(body.date),
            prompts_dict,
        )
    return JournalEntryResponse(
        id=row["id"],
        date=row["date"],
        prompts=JournalPrompts(**(row["prompts"] or {})),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
