"""Deck CRUD endpoints."""

import asyncpg
from fastapi import APIRouter, Depends, Request
from jarvis_common.auth import current_user_id_strict

from learning_engine.converters import row_to_deck_response
from learning_engine.deps import get_db_pool, limiter
from learning_engine.models import DeckCreate, DeckResponse

router = APIRouter(prefix="/api/decks", tags=["decks"])


@router.post("", response_model=DeckResponse, status_code=201)
@limiter.limit("30/minute")
async def create_deck(
    request: Request,
    body: DeckCreate,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(current_user_id_strict),
) -> DeckResponse:
    """Create a new flashcard deck."""
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO decks (name, description, topic_id, user_id)
            VALUES ($1, $2, $3, $4)
            RETURNING *, 0 AS card_count, 0 AS due_count
            """,
            body.name,
            body.description,
            body.topic_id,
            user_id,
        )
    return row_to_deck_response(row)


@router.get("", response_model=list[DeckResponse])
@limiter.limit("60/minute")
async def list_decks(
    request: Request,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(current_user_id_strict),
) -> list[DeckResponse]:
    """List all decks with card and due counts."""
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT d.*,
                   COUNT(c.id) AS card_count,
                   COUNT(c.id) FILTER (WHERE c.due_at <= NOW()) AS due_count
            FROM decks d
            LEFT JOIN cards c ON c.deck_id = d.id AND c.user_id = $1
            WHERE d.user_id = $1
            GROUP BY d.id
            ORDER BY d.created_at DESC
            """,
            user_id,
        )
    return [row_to_deck_response(row) for row in rows]
