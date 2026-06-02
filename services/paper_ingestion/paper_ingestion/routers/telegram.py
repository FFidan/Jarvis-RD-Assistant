"""Telegram pairing endpoints.

Per-user multi-tenant flow — ``POST /api/telegram/pair-token`` issues a
15-minute token tied to the authenticated user.  The bot's ``/pair <token>``
command consumes it and inserts a row into ``telegram_user_pairings``.
``GET /api/telegram/pairing`` and ``DELETE /api/telegram/pairing`` let users
inspect / revoke their own pairing.
"""

from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime, timedelta

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request
from jarvis_common import log_audit
from jarvis_common.auth import current_user_id_or_none
from pydantic import BaseModel

from paper_ingestion.deps import get_db_pool, limiter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/telegram", tags=["telegram"])

_PAIR_TOKEN_TTL = timedelta(minutes=15)


class PairTokenResponse(BaseModel):
    """Response body for ``POST /api/telegram/pair-token``."""

    token: str
    expires_at: datetime


class UserPairingStatus(BaseModel):
    """Response body for ``GET /api/telegram/pairing``."""

    paired: bool
    chat_id: int | None = None
    telegram_username: str | None = None
    paired_at: datetime | None = None


# ---------------------------------------------------------------------------
# Per-user multi-tenant pairing endpoints
# ---------------------------------------------------------------------------


@router.post("/pair-token", response_model=PairTokenResponse)
@limiter.limit("5/minute")
async def create_pair_token(
    request: Request,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int | None = Depends(current_user_id_or_none),
) -> PairTokenResponse:
    """Issue a 15-minute pairing token for the authenticated user.

    The token is inserted into ``telegram_pairing_tokens``.  The Telegram bot
    consumes it via ``/pair <token>`` and records the (user_id, chat_id) pair
    in ``telegram_user_pairings``.

    Requires an authenticated session (``user_id`` must be non-None).

    Rate-limited to 5 requests per minute per IP.
    """
    if user_id is None:
        raise HTTPException(
            status_code=401, detail="Authentication required to generate pairing token"
        )

    token = secrets.token_hex(16)  # 128-bit entropy, 32 hex chars
    expires_at = datetime.now(UTC) + _PAIR_TOKEN_TTL

    async with db_pool.acquire() as conn:
        # Delete any previous unconsumed tokens for this user to keep the table tidy.
        await conn.execute(
            "DELETE FROM telegram_pairing_tokens WHERE user_id = $1 AND consumed_at IS NULL",
            user_id,
        )
        await conn.execute(
            """INSERT INTO telegram_pairing_tokens (token, user_id, expires_at)
               VALUES ($1, $2, $3)""",
            token,
            user_id,
            expires_at,
        )

    await log_audit(
        db_pool,
        action="telegram_pair_token_issued",
        resource=f"telegram:pair_token:user:{user_id}",
        metadata={"expires_at": expires_at.isoformat()},
        user_id=str(user_id),
    )

    return PairTokenResponse(token=token, expires_at=expires_at)


@router.get("/pairing", response_model=UserPairingStatus)
async def get_user_pairing(
    request: Request,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int | None = Depends(current_user_id_or_none),
) -> UserPairingStatus:
    """Return the current user's Telegram pairing status.

    Reads from ``telegram_user_pairings``.  Returns ``paired=False`` when
    no pairing exists or the caller is unauthenticated.
    """
    if user_id is None:
        return UserPairingStatus(paired=False)

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT chat_id, telegram_username, paired_at
               FROM telegram_user_pairings
               WHERE user_id = $1""",
            user_id,
        )

    if row is None:
        return UserPairingStatus(paired=False)

    return UserPairingStatus(
        paired=True,
        chat_id=row["chat_id"],
        telegram_username=row["telegram_username"],
        paired_at=row["paired_at"],
    )


@router.delete("/pairing", status_code=204, response_model=None)
async def remove_user_pairing(
    request: Request,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int | None = Depends(current_user_id_or_none),
) -> None:
    """Remove the current user's Telegram pairing.

    Deletes the row from ``telegram_user_pairings`` and any unconsumed
    pairing tokens for the user.  No-op (204) if no pairing exists.

    Requires an authenticated session.
    """
    if user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required to remove pairing")

    async with db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "DELETE FROM telegram_user_pairings WHERE user_id = $1",
                user_id,
            )
            await conn.execute(
                "DELETE FROM telegram_pairing_tokens WHERE user_id = $1 AND consumed_at IS NULL",
                user_id,
            )

    await log_audit(
        db_pool,
        action="telegram_pairing_removed",
        resource=f"telegram:pairing:user:{user_id}",
        metadata={},
        user_id=str(user_id),
    )
