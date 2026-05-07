"""Today's Intent persistence — single row per (user, day)."""

from typing import TypedDict


class IntentRow(TypedDict):
    intent: str | None
    updated_at: str | None


async def get_today(pool, user_id: int | None) -> IntentRow:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT intent_text, updated_at FROM daily_intent "
            "WHERE (user_id IS NULL OR user_id IS NOT DISTINCT FROM $1)"
            " AND intent_date = CURRENT_DATE",
            user_id,
        )
    if not row:
        return {"intent": None, "updated_at": None}
    return {
        "intent": row["intent_text"],
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


async def upsert_today(pool, user_id: int | None, intent: str) -> IntentRow:
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


async def delete_today(pool, user_id: int | None) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM daily_intent"
            " WHERE (user_id IS NULL OR user_id IS NOT DISTINCT FROM $1)"
            " AND intent_date = CURRENT_DATE",
            user_id,
        )
