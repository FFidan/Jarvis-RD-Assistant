"""Telegram pairing endpoints.

Used by the setup wizard to generate a short-lived pairing code that the
user pastes into Telegram (`/start PAIR_<code>`). The bot then calls
`/api/telegram/pairing/confirm` (handled elsewhere) to persist
`telegram.owner_chat_id` in ``user_config``.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request
from jarvis_common import log_audit
from pydantic import BaseModel

from paper_ingestion.deps import get_db_pool, limiter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/telegram", tags=["telegram"])

_PAIRING_TTL = timedelta(minutes=10)
_TELEGRAM_BASE_URL = "https://t.me"

# Global cooldown between pairing-code generations across the whole instance.
# The slowapi ``@limiter.limit("10/minute")`` decorator below enforces a
# per-client-IP cap; the global cooldown defends against a distributed
# attempt to brute-force the code space by rotating IPs (slowapi cannot see
# across IPs). 5 seconds is short enough not to disrupt legitimate setup
# wizard retries while still cutting attack throughput by orders of magnitude.
_GLOBAL_PAIRING_COOLDOWN_SECONDS = 5.0
_pairing_cooldown_lock = asyncio.Lock()
_last_pairing_request_monotonic: float = 0.0


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
async def create_pairing(
    request: Request,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> PairingResponse:
    """Generate a pairing code, expire stale codes, return deep link.

    Changes from original:
    - Expire-only sweep instead of full table wipe (preserves concurrent callers' codes).
    - DELETE + INSERT wrapped in a single transaction so a crash cannot leave
      the table empty without a valid code.
    - Rate-limited to 10 requests per minute per IP **plus** a global
      :data:`_GLOBAL_PAIRING_COOLDOWN_SECONDS` cooldown that defends against
      a distributed brute-force where an attacker rotates source IPs to
      bypass the per-IP slowapi limit.
    - Pairing code now uses ``secrets.token_hex(8)`` (64-bit entropy, 16 hex
      chars) — up from 48-bit so even a fully throttled attacker cannot
      enumerate the code space within the 10-minute TTL.
    """
    # Global cooldown — defends against IP-rotating brute force.
    global _last_pairing_request_monotonic
    async with _pairing_cooldown_lock:
        now_mono = time.monotonic()
        elapsed = now_mono - _last_pairing_request_monotonic
        if elapsed < _GLOBAL_PAIRING_COOLDOWN_SECONDS:
            remaining = int(_GLOBAL_PAIRING_COOLDOWN_SECONDS - elapsed) + 1
            raise HTTPException(
                status_code=429,
                detail="Pairing code generation is globally rate-limited; "
                f"please wait {remaining}s before trying again.",
            )
        _last_pairing_request_monotonic = now_mono

    code = secrets.token_hex(8)  # 16 hex chars, 64-bit entropy
    expires_at = datetime.now(UTC) + _PAIRING_TTL

    async with db_pool.acquire() as conn:
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
        deep_link = f"{_TELEGRAM_BASE_URL}/{bot_username}?start=PAIR_{code}"
    else:
        deep_link = f"{_TELEGRAM_BASE_URL}/?start=PAIR_{code}"

    await log_audit(
        db_pool,
        action="telegram_pairing_created",
        resource="telegram:pairing",
        metadata={
            "expires_at": expires_at.isoformat(),
            "bot_username_missing": bot_username_missing,
        },
    )

    return PairingResponse(
        code=code,
        deep_link=deep_link,
        expires_at=expires_at,
        bot_username_missing=bot_username_missing,
    )


@router.get("/pairing/status", response_model=PairingStatus)
async def get_pairing_status(
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> PairingStatus:
    """Return whether ``telegram.owner_chat_id`` is set in user_config."""
    async with db_pool.acquire() as conn:
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
