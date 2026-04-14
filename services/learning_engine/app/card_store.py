"""Shared card persistence helpers.

This module keeps insert/update primitives out of router modules so generation
and CRUD endpoints can share the same persistence path without importing each
other.
"""

from __future__ import annotations

from datetime import datetime

import asyncpg


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
):
    """Insert a card row and return the created record."""
    return await conn.fetchrow(
        """INSERT INTO cards (deck_id, paper_id, card_type, front, back,
                              evidence, fsrs_state, due_at)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
           RETURNING *""",
        deck_id,
        paper_id,
        card_type,
        front,
        back,
        evidence,
        fsrs_state,
        due_at,
    )
