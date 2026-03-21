"""Deck CRUD endpoints."""

import asyncpg
from fastapi import APIRouter, Depends, Request

from app.converters import row_to_deck_response
from app.deps import get_db_pool, limiter
from app.models import DeckCreate, DeckResponse

router = APIRouter(tags=["decks"])


@router.post("/api/decks", response_model=DeckResponse, status_code=201)
@limiter.limit("30/minute")
async def create_deck(
    request: Request,
    body: DeckCreate,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> DeckResponse:
    """Create a new flashcard deck."""
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO decks (name, description, topic_id)
            VALUES ($1, $2, $3)
            RETURNING *, 0 AS card_count, 0 AS due_count
            """,
            body.name,
            body.description,
            body.topic_id,
        )
    return row_to_deck_response(row)


@router.get("/api/decks", response_model=list[DeckResponse])
@limiter.limit("60/minute")
async def list_decks(
    request: Request,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> list[DeckResponse]:
    """List all decks with card and due counts."""
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT d.*,
                   COUNT(c.id) AS card_count,
                   COUNT(c.id) FILTER (WHERE c.due_at <= NOW()) AS due_count
            FROM decks d
            LEFT JOIN cards c ON c.deck_id = d.id
            GROUP BY d.id
            ORDER BY d.created_at DESC
            """
        )
    return [row_to_deck_response(row) for row in rows]
