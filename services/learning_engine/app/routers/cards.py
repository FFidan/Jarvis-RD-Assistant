"""Card CRUD endpoints."""

from datetime import datetime

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.card_store import insert_card
from app.converters import row_to_card_response
from app.deps import get_db_pool, get_fsrs_manager, limiter
from app.fsrs_manager import FSRSManager
from app.models import CardCreate, CardResponse, CardUpdate

router = APIRouter(tags=["cards"])


@router.post("/api/cards", response_model=CardResponse, status_code=201)
@limiter.limit("30/minute")
async def create_card(
    request: Request,
    body: CardCreate,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    fsrs_manager: FSRSManager = Depends(get_fsrs_manager),
) -> CardResponse:
    """Create a flashcard manually."""
    async with db_pool.acquire() as conn:
        fsrs_state, due_at = fsrs_manager.create_new_card()
        evidence = body.evidence.model_dump() if body.evidence else {}

        try:
            row = await insert_card(
                conn,
                body.deck_id,
                body.paper_id,
                body.card_type.value,
                body.front,
                body.back,
                evidence,
                fsrs_state,
                due_at,
            )
        except asyncpg.ForeignKeyViolationError as exc:
            constraint = getattr(exc, "constraint_name", "") or ""
            if "paper" in constraint:
                raise HTTPException(status_code=404, detail="Paper not found") from None
            raise HTTPException(status_code=404, detail="Deck not found") from None
    return row_to_card_response(row)


@router.get("/api/cards", response_model=list[CardResponse])
@limiter.limit("60/minute")
async def list_cards(
    request: Request,
    deck_id: int | None = None,
    due_before: datetime | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> list[CardResponse]:
    """List cards with optional filters."""
    conditions: list[str] = []
    params: list = []
    param_idx = 1

    if deck_id is not None:
        conditions.append(f"deck_id = ${param_idx}")
        params.append(deck_id)
        param_idx += 1

    if due_before is not None:
        conditions.append(f"due_at <= ${param_idx}")
        params.append(due_before)
        param_idx += 1

    query = "SELECT * FROM cards"
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += f" ORDER BY due_at ASC NULLS LAST LIMIT ${param_idx} OFFSET ${param_idx + 1}"
    params.extend([limit, offset])

    async with db_pool.acquire() as conn:
        rows = await conn.fetch(query, *params)
    return [row_to_card_response(row) for row in rows]


@router.put("/api/cards/{card_id}", response_model=CardResponse)
@limiter.limit("30/minute")
async def update_card(
    request: Request,
    card_id: int,
    body: CardUpdate,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> CardResponse:
    """Update a card's content (does not affect FSRS state)."""
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            existing = await conn.fetchrow("SELECT * FROM cards WHERE id = $1 FOR UPDATE", card_id)
            if not existing:
                raise HTTPException(status_code=404, detail="Card not found")

            updates: list[str] = []
            params: list = []
            param_idx = 1

            if body.front is not None:
                updates.append(f"front = ${param_idx}")
                params.append(body.front)
                param_idx += 1
            if body.back is not None:
                updates.append(f"back = ${param_idx}")
                params.append(body.back)
                param_idx += 1
            if body.card_type is not None:
                updates.append(f"card_type = ${param_idx}")
                params.append(body.card_type.value)
                param_idx += 1
            if body.evidence is not None:
                updates.append(f"evidence = ${param_idx}")
                params.append(body.evidence.model_dump())
                param_idx += 1

            if not updates:
                return row_to_card_response(existing)

            updates.append("updated_at = NOW()")
            params.append(card_id)

            row = await conn.fetchrow(
                f"UPDATE cards SET {', '.join(updates)} WHERE id = ${param_idx} RETURNING *",  # nosec B608 - column names are hardcoded and values stay parameterized
                *params,
            )
    if not row:
        raise HTTPException(status_code=404, detail="Card not found")
    return row_to_card_response(row)


@router.delete("/api/cards/{card_id}", status_code=204)
@limiter.limit("30/minute")
async def delete_card(
    request: Request,
    card_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> None:
    """Delete a card."""
    async with db_pool.acquire() as conn:
        result = await conn.execute("DELETE FROM cards WHERE id = $1", card_id)
        if result == "DELETE 0":
            raise HTTPException(status_code=404, detail="Card not found")
