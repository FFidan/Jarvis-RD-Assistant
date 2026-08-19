"""Scoped Platform boundary used by the Telegram service principal."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from jarvis_common.config_validators import TIMER_DEFAULTS, TIMER_RANGES
from jarvis_common.crypto import resolve_secret_row
from jarvis_common.event_log import log_event
from jarvis_common.identity_assertions import IdentityAssertionSigner
from jarvis_common.identity_capabilities import (
    IdentityAudience,
    ServicePrincipal,
    service_principal_scopes,
)
from pydantic import BaseModel, Field, JsonValue

from platform_api.deps import (
    authenticate_service_principal,
    get_db_pool,
    get_identity_signer,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal/telegram", tags=["internal", "telegram"])

#: The focus-timer contract comes from the shared validator, so Platform can
#: never hand Telegram a value the web app would reject.
_WORK_MINUTES_RANGE = TIMER_RANGES["workMinutes"]
_TARGET_CYCLES_RANGE = TIMER_RANGES["targetCycles"]
_DEFAULT_WORK_MINUTES = TIMER_DEFAULTS["workMinutes"]
_DEFAULT_TARGET_CYCLES = TIMER_DEFAULTS["targetCycles"]

type TelegramPrincipal = Annotated[
    ServicePrincipal,
    Depends(authenticate_service_principal),
]
type DatabasePool = Annotated[asyncpg.Pool, Depends(get_db_pool)]
type AssertionSigner = Annotated[IdentityAssertionSigner, Depends(get_identity_signer)]


class TelegramAuthorizationRequest(BaseModel):
    """Request for one route-bound downstream Telegram assertion."""

    audience: IdentityAudience
    method: str = Field(min_length=1, max_length=16)
    path: str = Field(min_length=1, max_length=2048)
    request_id: str = Field(min_length=1, max_length=128)
    user_id: int = Field(gt=0)


class TelegramAuthorizationResponse(BaseModel):
    """Signed downstream identity returned to the Telegram client."""

    assertion: str
    user_id: int
    scopes: tuple[str, ...]


class PairingRequest(BaseModel):
    """Telegram pairing details supplied after a private-chat command."""

    token: str = Field(min_length=1, max_length=256)
    chat_id: int
    telegram_username: str | None = Field(default=None, max_length=255)


class PairingResult(BaseModel):
    """Atomic pairing outcome returned to the bot."""

    outcome: Literal["expired", "invalid", "paired", "used"]
    user_id: int | None = None
    prior_chat_id: int | None = None


class PairingRecord(BaseModel):
    """Platform-owned mapping between one JARVIS user and Telegram chat."""

    user_id: int
    chat_id: int
    telegram_username: str | None = None
    paired_at: datetime | None = None


class UnpairResult(BaseModel):
    """Idempotent Telegram unpair result."""

    removed: bool
    user_id: int | None = None


class TelegramConfigResponse(BaseModel):
    """Telegram runtime configuration resolved by Platform."""

    bot_token: str


class TelegramRuntimeResponse(BaseModel):
    """Platform-owned global scheduling context for Telegram."""

    owner_user_id: int | None = None
    owner_chat_id: int | None = None
    timezone: str = "UTC"


class TelegramTimerPreferences(BaseModel):
    """One user's saved focus-timer preference, resolved by Platform."""

    work_minutes: int = Field(ge=_WORK_MINUTES_RANGE[0], le=_WORK_MINUTES_RANGE[1])
    target_cycles: int = Field(ge=_TARGET_CYCLES_RANGE[0], le=_TARGET_CYCLES_RANGE[1])


class TelegramEventRequest(BaseModel):
    """Bounded semantic event emitted by the Telegram service."""

    level: Literal["debug", "info", "warning", "error", "critical"]
    category: Literal["error", "job", "source", "auth", "config"]
    message: str = Field(min_length=1, max_length=255)
    context: dict[str, JsonValue] = Field(default_factory=dict)
    correlation_id: uuid.UUID | None = None


@router.post("/authorize", response_model=TelegramAuthorizationResponse)
async def authorize_downstream_request(
    body: TelegramAuthorizationRequest,
    principal: TelegramPrincipal,
    db_pool: DatabasePool,
    signer: AssertionSigner,
) -> TelegramAuthorizationResponse:
    """Mint an exact, short-lived assertion for one paired user's command.

    Parameters
    ----------
    body : TelegramAuthorizationRequest
        Exact destination binding and paired JARVIS user.
    principal : {"learning", "research", "telegram"}
        Authenticated caller; only Telegram is accepted.
    db_pool : asyncpg.Pool
        Platform-owned database pool.
    signer : IdentityAssertionSigner
        Platform-only Ed25519 signer.

    Returns
    -------
    TelegramAuthorizationResponse
        Signed assertion and the verified user/scope binding.

    Raises
    ------
    HTTPException
        With status 400 for malformed bindings, 403 for the wrong service or a
        denied capability, and 404 when the user has no active pairing.
    """
    _require_telegram(principal)
    try:
        scopes = service_principal_scopes(
            "telegram",
            body.audience,
            body.method,
            body.path,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="Downstream request binding is invalid",
        ) from exc
    if scopes is None:
        raise HTTPException(status_code=403, detail="Telegram capability is not allowed")

    paired = await db_pool.fetchval(
        "SELECT EXISTS(SELECT 1 FROM telegram_user_pairings WHERE user_id = $1)",
        body.user_id,
    )
    if paired is not True:
        raise HTTPException(status_code=404, detail="Telegram pairing not found")

    assertion = signer.issue(
        audience=body.audience,
        subject=f"user:{body.user_id}",
        principal="telegram",
        user_id=body.user_id,
        request_id=body.request_id,
        request_method=body.method,
        request_path=body.path,
        scopes=scopes,
    )
    return TelegramAuthorizationResponse(
        assertion=assertion,
        user_id=body.user_id,
        scopes=scopes,
    )


@router.post("/pairings", response_model=PairingResult)
async def pair_chat(
    body: PairingRequest,
    principal: TelegramPrincipal,
    db_pool: DatabasePool,
) -> PairingResult:
    """Consume one pairing code and atomically bind its user to a chat.

    Parameters
    ----------
    body : PairingRequest
        Pairing code and private-chat metadata.
    principal : {"learning", "research", "telegram"}
        Authenticated caller; only Telegram is accepted.
    db_pool : asyncpg.Pool
        Platform-owned database pool.

    Returns
    -------
    PairingResult
        Stable outcome including rebound information when another chat was
        displaced.

    Raises
    ------
    HTTPException
        With status 403 for the wrong service or 409 when the chat already
        belongs to another account.
    """
    _require_telegram(principal)
    try:
        async with db_pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """SELECT user_id, expires_at, consumed_at
                   FROM telegram_pairing_tokens
                   WHERE token = $1
                   FOR UPDATE""",
                body.token,
            )
            if row is None:
                return PairingResult(outcome="invalid")
            if row["consumed_at"] is not None:
                return PairingResult(outcome="used")
            if row["expires_at"] < datetime.now(UTC):
                await conn.execute(
                    "DELETE FROM telegram_pairing_tokens WHERE token = $1",
                    body.token,
                )
                return PairingResult(outcome="expired")

            user_id = int(row["user_id"])
            upserted = await conn.fetchrow(
                """WITH previous AS (
                       SELECT chat_id
                       FROM telegram_user_pairings
                       WHERE user_id = $1
                       FOR UPDATE
                   )
                   INSERT INTO telegram_user_pairings
                          (user_id, chat_id, telegram_username, paired_at)
                   VALUES ($1, $2, $3, NOW())
                   ON CONFLICT (user_id) DO UPDATE
                       SET chat_id = EXCLUDED.chat_id,
                           telegram_username = EXCLUDED.telegram_username,
                           paired_at = NOW()
                   RETURNING
                       (xmax <> 0) AS was_update,
                       (SELECT chat_id FROM previous) AS prior_chat_id""",
                user_id,
                body.chat_id,
                body.telegram_username,
            )
            await conn.execute(
                "UPDATE telegram_pairing_tokens SET consumed_at = NOW() WHERE token = $1",
                body.token,
            )
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Telegram chat is already paired to another account",
        ) from exc

    prior_chat_id = (
        int(upserted["prior_chat_id"])
        if upserted is not None and upserted["was_update"] and upserted["prior_chat_id"] is not None
        else None
    )
    if prior_chat_id is not None and prior_chat_id != body.chat_id:
        await log_event(
            pool=db_pool,
            level="warning",
            category="auth",
            source="telegram_bot",
            message="pairing.rebound",
            context={
                "user_id": user_id,
                "prior_chat_id": prior_chat_id,
                "new_chat_id": body.chat_id,
            },
        )
    return PairingResult(
        outcome="paired",
        user_id=user_id,
        prior_chat_id=prior_chat_id,
    )


@router.get("/pairings", response_model=list[PairingRecord])
async def list_pairings(
    principal: TelegramPrincipal,
    db_pool: DatabasePool,
) -> list[PairingRecord]:
    """Return all active pairings for Telegram delivery orchestration.

    Parameters
    ----------
    principal : {"learning", "research", "telegram"}
        Authenticated caller; only Telegram is accepted.
    db_pool : asyncpg.Pool
        Platform-owned database pool.

    Returns
    -------
    list[PairingRecord]
        Active pairings ordered by JARVIS user identifier.
    """
    _require_telegram(principal)
    rows = await db_pool.fetch(
        """SELECT user_id, chat_id, telegram_username, paired_at
           FROM telegram_user_pairings
           ORDER BY user_id"""
    )
    return [PairingRecord.model_validate(dict(row)) for row in rows]


@router.get("/pairings/{chat_id}", response_model=PairingRecord)
async def resolve_pairing(
    chat_id: int,
    principal: TelegramPrincipal,
    db_pool: DatabasePool,
) -> PairingRecord:
    """Resolve one Telegram chat to its paired JARVIS user.

    Parameters
    ----------
    chat_id : int
        Telegram chat identifier.
    principal : {"learning", "research", "telegram"}
        Authenticated caller; only Telegram is accepted.
    db_pool : asyncpg.Pool
        Platform-owned database pool.

    Returns
    -------
    PairingRecord
        Verified pairing record.

    Raises
    ------
    HTTPException
        With status 403 for the wrong service or 404 when unpaired.
    """
    _require_telegram(principal)
    row = await db_pool.fetchrow(
        """SELECT user_id, chat_id, telegram_username, paired_at
           FROM telegram_user_pairings
           WHERE chat_id = $1""",
        chat_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Telegram pairing not found")
    return PairingRecord.model_validate(dict(row))


@router.delete("/pairings/{chat_id}", response_model=UnpairResult)
async def unpair_chat(
    chat_id: int,
    principal: TelegramPrincipal,
    db_pool: DatabasePool,
) -> UnpairResult:
    """Remove one chat pairing and outstanding codes idempotently.

    Parameters
    ----------
    chat_id : int
        Telegram chat identifier.
    principal : {"learning", "research", "telegram"}
        Authenticated caller; only Telegram is accepted.
    db_pool : asyncpg.Pool
        Platform-owned database pool.

    Returns
    -------
    UnpairResult
        Whether a pairing was removed and its former user identifier.
    """
    _require_telegram(principal)
    async with db_pool.acquire() as conn, conn.transaction():
        user_id = await conn.fetchval(
            "DELETE FROM telegram_user_pairings WHERE chat_id = $1 RETURNING user_id",
            chat_id,
        )
        if user_id is not None:
            await conn.execute(
                "DELETE FROM telegram_pairing_tokens WHERE user_id = $1 AND consumed_at IS NULL",
                int(user_id),
            )
    return UnpairResult(removed=user_id is not None, user_id=int(user_id) if user_id else None)


@router.get("/config", response_model=TelegramConfigResponse)
async def get_telegram_config(
    principal: TelegramPrincipal,
    db_pool: DatabasePool,
) -> TelegramConfigResponse:
    """Return the Platform-decrypted Telegram token to the bot.

    Parameters
    ----------
    principal : {"learning", "research", "telegram"}
        Authenticated caller; only Telegram is accepted.
    db_pool : asyncpg.Pool
        Platform-owned database pool.

    Returns
    -------
    TelegramConfigResponse
        Decrypted bot token.

    Raises
    ------
    HTTPException
        With status 403 for the wrong service or 404 when no token is stored.
    """
    _require_telegram(principal)
    row = await db_pool.fetchrow(
        """SELECT value, encrypted_value
           FROM user_config
           WHERE key = 'telegram.bot_token' AND user_id IS NULL"""
    )
    token = resolve_secret_row(row) if row is not None else None
    if not token:
        raise HTTPException(status_code=404, detail="Telegram bot token is not configured")
    return TelegramConfigResponse(bot_token=token)


@router.get("/runtime", response_model=TelegramRuntimeResponse)
async def get_telegram_runtime(
    principal: TelegramPrincipal,
    db_pool: DatabasePool,
) -> TelegramRuntimeResponse:
    """Return Platform-owned owner and timezone scheduling context.

    Parameters
    ----------
    principal : {"learning", "research", "telegram"}
        Authenticated caller; only Telegram is accepted.
    db_pool : asyncpg.Pool
        Platform-owned database pool.

    Returns
    -------
    TelegramRuntimeResponse
        Owner pairing and effective timezone, with UTC as the safe fallback.
    """
    _require_telegram(principal)
    owner_chat_raw = await db_pool.fetchval(
        "SELECT value FROM user_config WHERE key = 'telegram.owner_chat_id' AND user_id IS NULL"
    )
    try:
        owner_chat_id = int(owner_chat_raw) if owner_chat_raw is not None else None
    except (TypeError, ValueError):
        logger.warning("Ignoring invalid telegram.owner_chat_id runtime configuration")
        owner_chat_id = None

    owner_user_id: int | None = None
    if owner_chat_id is not None:
        resolved_user = await db_pool.fetchval(
            "SELECT user_id FROM telegram_user_pairings WHERE chat_id = $1",
            owner_chat_id,
        )
        if resolved_user is not None:
            owner_user_id = int(resolved_user)

    timezone = None
    if owner_user_id is not None:
        timezone = await db_pool.fetchval(
            "SELECT value FROM user_config WHERE key = 'user.timezone' AND user_id = $1",
            owner_user_id,
        )
    if not timezone:
        timezone = await db_pool.fetchval(
            "SELECT value FROM user_config WHERE key = 'user.timezone' AND user_id IS NULL"
        )
    return TelegramRuntimeResponse(
        owner_user_id=owner_user_id,
        owner_chat_id=owner_chat_id,
        timezone=str(timezone) if timezone else "UTC",
    )


@router.get("/preferences/{user_id}/timer", response_model=TelegramTimerPreferences)
async def get_timer_preferences(
    user_id: int,
    principal: TelegramPrincipal,
    db_pool: DatabasePool,
) -> TelegramTimerPreferences:
    """Return one user's saved focus-timer length and daily cycle target.

    Parameters
    ----------
    user_id : int
        JARVIS user whose personal ``ui.timer`` preference is read.
    principal : {"learning", "research", "telegram"}
        Authenticated caller; only Telegram is accepted.
    db_pool : asyncpg.Pool
        Platform-owned database pool.

    Returns
    -------
    TelegramTimerPreferences
        The saved values, or the web app's defaults when no preference is
        stored or the stored one is unusable. A user without a preference is
        not an error: the bot has to answer with the same length the web timer
        would have used.
    """
    _require_telegram(principal)
    stored = await db_pool.fetchval(
        "SELECT value FROM user_config WHERE key = 'ui.timer' AND user_id = $1",
        user_id,
    )
    return TelegramTimerPreferences(
        work_minutes=_timer_field(
            stored, "workMinutes", _DEFAULT_WORK_MINUTES, _WORK_MINUTES_RANGE
        ),
        target_cycles=_timer_field(
            stored, "targetCycles", _DEFAULT_TARGET_CYCLES, _TARGET_CYCLES_RANGE
        ),
    )


@router.post("/events", status_code=status.HTTP_204_NO_CONTENT)
async def record_telegram_event(
    body: TelegramEventRequest,
    principal: TelegramPrincipal,
    db_pool: DatabasePool,
) -> None:
    """Persist one bounded Telegram semantic event in Platform.

    Parameters
    ----------
    body : TelegramEventRequest
        Validated event payload.
    principal : {"learning", "research", "telegram"}
        Authenticated caller; only Telegram is accepted.
    db_pool : asyncpg.Pool
        Platform-owned database pool.
    """
    _require_telegram(principal)
    await log_event(
        pool=db_pool,
        level=body.level,
        category=body.category,
        source="telegram_bot",
        message=body.message,
        context=body.context,
        correlation_id=body.correlation_id,
    )


def _timer_field(
    stored: object,
    field: str,
    default: int,
    bounds: tuple[int, int],
) -> int:
    """Read one integer out of a stored ``ui.timer`` value, or fall back.

    The row is written by the web app and is only validated on that write path,
    so an out-of-range or wrongly typed field is treated as absent rather than
    surfaced as an error to the bot.
    """
    if isinstance(stored, str):
        try:
            stored = json.loads(stored)
        except ValueError:
            logger.warning("Ignoring unparsable ui.timer preference")
            return default
    if not isinstance(stored, dict):
        return default
    value = stored.get(field)
    minimum, maximum = bounds
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        logger.warning("Ignoring invalid ui.timer.%s preference", field)
        return default
    return value


def _require_telegram(principal: ServicePrincipal) -> None:
    if principal != "telegram":
        raise HTTPException(status_code=403, detail="Telegram service capability is required")


__all__ = [
    "PairingRecord",
    "PairingRequest",
    "PairingResult",
    "TelegramAuthorizationRequest",
    "TelegramAuthorizationResponse",
    "TelegramConfigResponse",
    "TelegramEventRequest",
    "TelegramRuntimeResponse",
    "TelegramTimerPreferences",
    "UnpairResult",
    "authorize_downstream_request",
    "get_telegram_config",
    "get_telegram_runtime",
    "get_timer_preferences",
    "list_pairings",
    "pair_chat",
    "record_telegram_event",
    "resolve_pairing",
    "router",
    "unpair_chat",
]
