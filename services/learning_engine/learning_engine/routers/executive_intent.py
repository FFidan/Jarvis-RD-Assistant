"""Today's Intent router (mounted under /api/executive/intent/*)."""

from asyncpg import Pool
from fastapi import APIRouter, Depends, Request
from jarvis_common.auth import current_user_id_strict

from learning_engine.deps import get_db_pool, limiter
from learning_engine.models import IntentBody
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

router = APIRouter(prefix="/api/executive", tags=["executive"])

# verify_api_key is applied globally on the FastAPI app (main.py:160) —
# the per-route dependency here would be redundant.


@router.get("/intent/today", response_model=IntentRow)
@limiter.limit("60/minute")
async def get_intent_today(
    request: Request,
    db_pool: Pool = Depends(get_db_pool),
    user_id: int = Depends(current_user_id_strict),
) -> IntentRow:
    """Return today's intent text for the current user."""
    return await _get_intent_today(db_pool, user_id)


@router.post("/intent/today", response_model=IntentRow)
@limiter.limit("30/minute")
async def save_intent_today(
    request: Request,
    payload: IntentBody,
    db_pool: Pool = Depends(get_db_pool),
    user_id: int = Depends(current_user_id_strict),
) -> IntentRow:
    """Upsert today's intent. Empty string clears (DELETE) the intent."""
    text = payload.intent.strip()
    if not text:
        await _delete_intent_today(db_pool, user_id)
        return {"intent": None, "updated_at": None}
    return await _upsert_intent_today(db_pool, user_id, text)
