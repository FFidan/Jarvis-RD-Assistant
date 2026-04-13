"""Telegram pairing endpoints.

Used by the setup wizard to generate a short-lived pairing code that the
user pastes into Telegram (`/start PAIR_<code>`). The bot then calls
`/api/telegram/pairing/confirm` (handled elsewhere) to persist
`telegram.owner_chat_id` in ``user_config``.
"""

from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Request
from jarvis_common import verify_api_key
from pydantic import BaseModel

from app.deps import limiter

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/telegram",
    tags=["telegram"],
    dependencies=[Depends(verify_api_key)],
)

_PAIRING_TTL = timedelta(minutes=10)


class PairingResponse(BaseModel):
    code: str
    deep_link: str
    expires_at: datetime
    bot_username_missing: bool = False


class PairingStatus(BaseModel):
    paired: bool
    chat_id: int | None = None


def _extract_bot_username(value: Any) -> str | None:
    """Return the bot username stored in ``user_config['telegram.bot_username']``.

    asyncpg decodes JSONB automatically, so the value is either ``None``,
    a ``dict`` (``{"username": ..., "set_at": ...}``) or legacy ``str``.
    """
    if value is None:
        return None
    if isinstance(value, dict):
        username = value.get("username")
        return username if isinstance(username, str) and username else None
    if isinstance(value, str) and value and value.lower() != "null":
        return value
    return None


@router.post("/pairing", response_model=PairingResponse)
@limiter.limit("10/minute")
async def create_pairing(request: Request) -> PairingResponse:
    """Generate a pairing code, expire stale codes, return deep link.

    Changes from original:
    - Expire-only sweep instead of full table wipe (preserves concurrent callers' codes).
    - DELETE + INSERT wrapped in a single transaction so a crash cannot leave
      the table empty without a valid code.
    - Rate-limited to 10 requests per minute per IP.
    """
    code = secrets.token_hex(6)  # 12 hex chars
    expires_at = datetime.now(UTC) + _PAIRING_TTL

    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Expire-only sweep — preserve valid codes from concurrent callers.
            await conn.execute("DELETE FROM telegram_pairing WHERE expires_at < NOW()")
            await conn.execute(
                "INSERT INTO telegram_pairing (code, expires_at) VALUES ($1, $2)",
                code,
                expires_at,
            )
        row = await conn.fetchrow(
            "SELECT value FROM user_config WHERE key = $1",
            "telegram.bot_username",
        )

    bot_username = _extract_bot_username(row["value"]) if row else None
    bot_username_missing = bot_username is None
    if bot_username:
        deep_link = f"https://t.me/{bot_username}?start=PAIR_{code}"
    else:
        deep_link = f"https://t.me/?start=PAIR_{code}"

    return PairingResponse(
        code=code,
        deep_link=deep_link,
        expires_at=expires_at,
        bot_username_missing=bot_username_missing,
    )


@router.get("/pairing/status", response_model=PairingStatus)
async def get_pairing_status(request: Request) -> PairingStatus:
    """Return whether ``telegram.owner_chat_id`` is set in user_config."""
    pool = request.app.state.db_pool
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT value FROM user_config WHERE key = $1",
            "telegram.owner_chat_id",
        )

    if row is None:
        return PairingStatus(paired=False, chat_id=None)

    value = row["value"]
    # asyncpg JSONB codec may decode 'null'::jsonb into Python None.
    if value is None:
        return PairingStatus(paired=False, chat_id=None)
    # Defensive: tolerate the literal string "null" which may appear if a
    # caller wrote json.dumps(None) via an older code path.
    if isinstance(value, str) and value.lower() == "null":
        return PairingStatus(paired=False, chat_id=None)
    if isinstance(value, int) and not isinstance(value, bool):
        return PairingStatus(paired=True, chat_id=value)
    logger.warning(
        "telegram.owner_chat_id has unexpected type %s; treating as unpaired",
        type(value).__name__,
    )
    return PairingStatus(paired=False, chat_id=None)
