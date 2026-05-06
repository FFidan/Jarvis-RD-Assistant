"""Today's Intent router (mounted under /api/executive/intent/*)."""

from __future__ import annotations

import os

from asyncpg import Pool
from fastapi import APIRouter, Depends
from jarvis_common.auth import verify_api_key
from pydantic import BaseModel, Field

from learning_engine.deps import get_db_pool
from learning_engine.repos.intent_repo import (
    IntentRow,
)
from learning_engine.repos.intent_repo import (
    delete_today as _delete_intent_today,
)
from learning_engine.repos.intent_repo import (
    get_today as _get_intent_today,
)
from learning_engine.repos.intent_repo import (
    upsert_today as _upsert_intent_today,
)

DEFAULT_USER_ID = os.environ.get("JARVIS_USER_ID", "default")

router = APIRouter(prefix="/api/executive", tags=["executive"])


class IntentBody(BaseModel):
    intent: str = Field(..., max_length=280)


@router.get("/intent/today", dependencies=[Depends(verify_api_key)])
async def get_intent_today(
    db_pool: Pool = Depends(get_db_pool),
) -> IntentRow:
    """Return today's intent text for the default user."""
    return await _get_intent_today(db_pool, DEFAULT_USER_ID)


@router.post("/intent/today", dependencies=[Depends(verify_api_key)])
async def save_intent_today(
    payload: IntentBody,
    db_pool: Pool = Depends(get_db_pool),
) -> IntentRow:
    """Upsert today's intent. Empty string clears (DELETE) the intent."""
    text = payload.intent.strip()
    if not text:
        await _delete_intent_today(db_pool, DEFAULT_USER_ID)
        return {"intent": None, "updated_at": None}
    return await _upsert_intent_today(db_pool, DEFAULT_USER_ID, text)
