"""Shared card persistence helpers.

This module keeps insert/update primitives out of router modules so generation
and CRUD endpoints can share the same persistence path without importing each
other.
"""

from __future__ import annotations

from datetime import datetime

import asyncpg

CURRENT_CARD_SQL = "(c.paper_id IS NULL OR c.content_generation = p.content_generation)"
CARD_STALE_SQL = (
    "(c.paper_id IS NOT NULL AND c.content_generation IS DISTINCT FROM p.content_generation)"
)


async def insert_card(
    conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
    deck_id: int,
    paper_id: int | None,
    card_type: str,
    front: str,
    back: str,
    evidence: dict | None,
    fsrs_state: dict,
    due_at: datetime,
    user_id: int | None = None,
    content_generation: int = 0,
) -> asyncpg.Record | None:
    """Insert a card row and return the created record.

    ``user_id`` (added by migration 070) is written as NULL when the caller
    has no resolved per-user identity (single-tenant or system path).
    """
    return await conn.fetchrow(
        """INSERT INTO cards (deck_id, paper_id, card_type, front, back,
                              evidence, fsrs_state, due_at, user_id,
                              content_generation)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
           RETURNING *""",
        deck_id,
        paper_id,
        card_type,
        front,
        back,
        evidence,
        fsrs_state,
        due_at,
        user_id,
        content_generation,
    )
