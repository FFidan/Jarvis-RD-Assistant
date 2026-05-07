"""Card CRUD endpoints."""

from datetime import datetime

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from jarvis_common import ErrorResponse, log_audit
from jarvis_common.auth import current_user_id_or_none
from jarvis_common.db_helpers import dynamic_update

from learning_engine.card_store import insert_card
from learning_engine.converters import row_to_card_response
from learning_engine.deps import get_db_pool, get_fsrs_manager, limiter
from learning_engine.fsrs_manager import FSRSManager
from learning_engine.models import CardCreate, CardResponse, CardUpdate

_CARD_ALLOWED_COLUMNS: frozenset[str] = frozenset({"front", "back", "card_type", "evidence"})
_CARD_JSONB_COLUMNS: frozenset[str] = frozenset({"evidence"})

router = APIRouter(
    prefix="/api/cards",
    tags=["cards"],
    responses={
        400: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
)


@router.post("", response_model=CardResponse, status_code=201)
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


@router.get("", response_model=list[CardResponse])
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

    if deck_id is not None:
        conditions.append(f"deck_id = ${len(params) + 1}")
        params.append(deck_id)

    if due_before is not None:
        conditions.append(f"due_at <= ${len(params) + 1}")
        params.append(due_before)

    query = "SELECT * FROM cards"
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += f" ORDER BY due_at ASC NULLS LAST LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}"
    params.extend([limit, offset])

    async with db_pool.acquire() as conn:
        rows = await conn.fetch(query, *params)
    return [row_to_card_response(row) for row in rows]


@router.put("/{card_id}", response_model=CardResponse)
@limiter.limit("30/minute")
async def update_card(
    request: Request,
    card_id: int,
    body: CardUpdate,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> CardResponse:
    """Update a card's content (does not affect FSRS state)."""
    async with db_pool.acquire() as conn:
        existing = await conn.fetchrow("SELECT id FROM cards WHERE id = $1", card_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Card not found")

        update_dict: dict = {}
        if body.front is not None:
            update_dict["front"] = body.front
        if body.back is not None:
            update_dict["back"] = body.back
        if body.card_type is not None:
            update_dict["card_type"] = body.card_type.value
        if body.evidence is not None:
            update_dict["evidence"] = body.evidence.model_dump()

        if not update_dict:
            row = await conn.fetchrow("SELECT * FROM cards WHERE id = $1", card_id)
            if not row:
                raise HTTPException(status_code=404, detail="Card not found")
            return row_to_card_response(row)

        row = await dynamic_update(
            conn,
            table="cards",
            record_id=card_id,
            updates=update_dict,
            allowed_columns=_CARD_ALLOWED_COLUMNS,
            jsonb_columns=_CARD_JSONB_COLUMNS,
            extra_sets=["updated_at = NOW()"],
        )
    if not row:
        raise HTTPException(status_code=404, detail="Card not found")
    return row_to_card_response(row)


@router.delete("/{card_id}", status_code=204)
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
    user_id = await current_user_id_or_none(request)
    await log_audit(
        db_pool,
        action="delete",
        resource=f"card:{card_id}",
        user_id=str(user_id) if user_id is not None else None,
    )
