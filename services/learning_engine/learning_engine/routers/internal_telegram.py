"""Learning-owned nudge commands for the Telegram service principal."""

from __future__ import annotations

from typing import Annotated

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from learning_engine.deps import get_db_pool

router = APIRouter(prefix="/internal/telegram", tags=["internal", "telegram"])
type DatabasePool = Annotated[asyncpg.Pool, Depends(get_db_pool)]


class ScheduledNudge(BaseModel):
    """Learning-owned enabled Telegram nudge schedule."""

    id: int
    nudge_type: str
    cron_expression: str


def require_telegram_principal(request: Request) -> None:
    """Require a verified Telegram service assertion.

    Parameters
    ----------
    request : Request
        Request whose signed identity middleware populated service state.

    Raises
    ------
    HTTPException
        With status 403 unless the verified principal is Telegram.
    """
    if getattr(request.state, "identity_principal", None) != "telegram":
        raise HTTPException(status_code=403, detail="Telegram service capability is required")


@router.get(
    "/nudges",
    response_model=list[ScheduledNudge],
    dependencies=[Depends(require_telegram_principal)],
)
async def list_enabled_nudges(db_pool: DatabasePool) -> list[ScheduledNudge]:
    """Return enabled Learning-owned nudge schedules.

    Parameters
    ----------
    db_pool : asyncpg.Pool
        Learning database pool.

    Returns
    -------
    list[ScheduledNudge]
        Enabled schedules ordered by identifier.
    """
    rows = await db_pool.fetch(
        """SELECT id, nudge_type, cron_expression
           FROM scheduled_nudges
           WHERE enabled = TRUE
           ORDER BY id"""
    )
    return [ScheduledNudge.model_validate(dict(row)) for row in rows]


@router.post(
    "/nudges/{nudge_id}/ack",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_telegram_principal)],
)
async def acknowledge_nudge(nudge_id: int, db_pool: DatabasePool) -> None:
    """Record successful execution of one Learning-owned nudge.

    Parameters
    ----------
    nudge_id : int
        Scheduled nudge identifier.
    db_pool : asyncpg.Pool
        Learning database pool.

    Raises
    ------
    HTTPException
        With status 404 when the nudge no longer exists.
    """
    updated = await db_pool.fetchval(
        "UPDATE scheduled_nudges SET last_fired_at = NOW() WHERE id = $1 RETURNING id",
        nudge_id,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="Scheduled nudge not found")


__all__ = [
    "ScheduledNudge",
    "acknowledge_nudge",
    "list_enabled_nudges",
    "require_telegram_principal",
    "router",
]
