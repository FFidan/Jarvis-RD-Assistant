"""Today's Intent persistence — single row per (user, day)."""

from typing import TypedDict

import asyncpg


class IntentRow(TypedDict):
    intent: str | None
    updated_at: str | None


async def get_today(pool: asyncpg.Pool, user_id: int | None) -> IntentRow:
    """Return today's intent for *user_id*, or ``{intent: None, updated_at: None}`` if absent.

    Parameters
    ----------
    pool :
        asyncpg connection pool.
    user_id : int | None
        Caller's user ID.  ``None`` is treated as the system/single-tenant row
        via ``IS NOT DISTINCT FROM`` semantics.

    Returns
    -------
    IntentRow
        ``{intent: str | None, updated_at: str | None}`` where ``updated_at``
        is an ISO-8601 string when present.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT intent_text, updated_at FROM daily_intent "
            "WHERE user_id IS NOT DISTINCT FROM $1"
            " AND intent_date = CURRENT_DATE",
            user_id,
        )
    if not row:
        return {"intent": None, "updated_at": None}
    return {
        "intent": row["intent_text"],
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


async def upsert_today(pool: asyncpg.Pool, user_id: int | None, intent: str) -> IntentRow:
    """Insert or update today's intent text for *user_id*.

    Parameters
    ----------
    pool :
        asyncpg connection pool.
    user_id : int | None
        Caller's user ID.
    intent : str
        Non-empty intent string (the caller is responsible for stripping
        whitespace and rejecting empty values before calling this function).

    Returns
    -------
    IntentRow
        The persisted ``{intent, updated_at}`` row.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO daily_intent (user_id, intent_date, intent_text, updated_at)
            VALUES ($1, CURRENT_DATE, $2, NOW())
            ON CONFLICT (user_id, intent_date) DO UPDATE
              SET intent_text = EXCLUDED.intent_text,
                  updated_at  = NOW()
            RETURNING intent_text, updated_at
            """,
            user_id,
            intent,
        )
    return {
        "intent": row["intent_text"],
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


async def delete_today(pool: asyncpg.Pool, user_id: int | None) -> None:
    """Delete today's intent row for *user_id*, if it exists.

    Parameters
    ----------
    pool :
        asyncpg connection pool.
    user_id : int | None
        Caller's user ID.  No-op when no row matches.
    """
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM daily_intent"
            " WHERE user_id IS NOT DISTINCT FROM $1"
            " AND intent_date = CURRENT_DATE",
            user_id,
        )
