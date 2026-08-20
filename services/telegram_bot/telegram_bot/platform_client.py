"""Typed Telegram client for Platform-owned pairing and event APIs."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Literal

import httpx

from telegram_bot.config import BotConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class UserPairing:
    """Resolved Platform pairing for one Telegram chat.

    Parameters
    ----------
    user_id : int
        JARVIS user identifier.
    chat_id : int
        Telegram private-chat identifier.
    telegram_username : str or None, optional
        Telegram username captured during pairing.
    paired_at : str or None, optional
        ISO timestamp returned by Platform.
    """

    user_id: int
    chat_id: int
    telegram_username: str | None = None
    paired_at: str | None = None


@dataclass(frozen=True, slots=True)
class PairingOutcome:
    """Atomic Platform pairing result.

    Parameters
    ----------
    outcome : {"expired", "invalid", "paired", "used"}
        Stable pairing outcome.
    user_id : int or None, optional
        Paired JARVIS user for a successful result.
    prior_chat_id : int or None, optional
        Displaced chat that should receive a best-effort security notice.
    """

    outcome: Literal["expired", "invalid", "paired", "used"]
    user_id: int | None = None
    prior_chat_id: int | None = None


@dataclass(frozen=True, slots=True)
class TelegramRuntime:
    """Platform-owned global Telegram scheduling context.

    Parameters
    ----------
    owner_user_id : int or None
        Paired JARVIS owner used to scope Learning nudge commands.
    owner_chat_id : int or None
        Telegram chat receiving operator-only failure notices.
    timezone : str
        Effective IANA timezone with Platform's UTC fallback applied.
    """

    owner_user_id: int | None
    owner_chat_id: int | None
    timezone: str


@dataclass(frozen=True, slots=True)
class TimerPreferences:
    """One user's saved focus-timer preference.

    Parameters
    ----------
    work_minutes : int
        Length of a single focus block, as configured in the web app.
    target_cycles : int
        How many focus blocks the user aims to complete in a day.
    """

    work_minutes: int
    target_cycles: int


async def resolve_pairing(
    client: httpx.AsyncClient,
    config: BotConfig,
    chat_id: int,
) -> UserPairing | None:
    """Resolve a private chat to its Platform-owned pairing.

    Parameters
    ----------
    client : httpx.AsyncClient
        Dedicated Platform client.
    config : BotConfig
        Runtime configuration containing the Platform origin.
    chat_id : int
        Telegram private-chat identifier.

    Returns
    -------
    UserPairing or None
        Pairing record, or ``None`` when the chat is unpaired.

    Raises
    ------
    httpx.HTTPStatusError
        If Platform returns an unexpected error.
    """
    response = await client.get(f"{config.platform_api_url}/internal/telegram/pairings/{chat_id}")
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return _parse_pairing(response.json())


async def list_user_pairings(
    client: httpx.AsyncClient,
    config: BotConfig,
) -> list[UserPairing]:
    """Return every active pairing for scheduled delivery.

    Parameters
    ----------
    client : httpx.AsyncClient
        Dedicated Platform client.
    config : BotConfig
        Runtime configuration containing the Platform origin.

    Returns
    -------
    list[UserPairing]
        Active pairings ordered by user identifier.

    Raises
    ------
    httpx.HTTPStatusError
        If Platform rejects or cannot serve the request.
    """
    response = await client.get(f"{config.platform_api_url}/internal/telegram/pairings")
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise RuntimeError("Platform returned an invalid pairing list")
    return [_parse_pairing(item) for item in payload]


async def get_runtime_context(
    client: httpx.AsyncClient,
    config: BotConfig,
) -> TelegramRuntime:
    """Return Platform-owned owner and timezone scheduling context.

    Parameters
    ----------
    client : httpx.AsyncClient
        Dedicated Platform client.
    config : BotConfig
        Runtime configuration containing the Platform origin.

    Returns
    -------
    TelegramRuntime
        Validated runtime scheduling context.

    Raises
    ------
    httpx.HTTPStatusError
        If Platform rejects or cannot serve the request.
    RuntimeError
        If Platform returns a malformed response.
    """
    response = await client.get(f"{config.platform_api_url}/internal/telegram/runtime")
    response.raise_for_status()
    payload = response.json()
    owner_user_id = _optional_int(payload.get("owner_user_id"))
    owner_chat_id = _optional_int(payload.get("owner_chat_id"))
    timezone = payload.get("timezone")
    if not isinstance(timezone, str) or not timezone:
        raise RuntimeError("Platform returned an invalid Telegram runtime context")
    return TelegramRuntime(owner_user_id, owner_chat_id, timezone)


async def get_timer_preferences(
    client: httpx.AsyncClient,
    config: BotConfig,
    user_id: int,
) -> TimerPreferences:
    """Return one user's saved focus-timer preference.

    Parameters
    ----------
    client : httpx.AsyncClient
        Dedicated Platform client.
    config : BotConfig
        Runtime configuration containing the Platform origin.
    user_id : int
        Paired JARVIS user whose preference is read.

    Returns
    -------
    TimerPreferences
        Validated timer preference; Platform substitutes the web app's
        defaults when the user has never saved one.

    Raises
    ------
    httpx.HTTPStatusError
        If Platform rejects or cannot serve the request.
    RuntimeError
        If Platform returns a malformed response.
    """
    response = await client.get(
        f"{config.platform_api_url}/internal/telegram/preferences/{user_id}/timer"
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("Platform returned an invalid timer preference")
    work_minutes = _optional_int(payload.get("work_minutes"))
    target_cycles = _optional_int(payload.get("target_cycles"))
    if work_minutes is None or target_cycles is None:
        raise RuntimeError("Platform returned an invalid timer preference")
    return TimerPreferences(work_minutes, target_cycles)


async def pair_chat(
    client: httpx.AsyncClient,
    config: BotConfig,
    *,
    token: str,
    chat_id: int,
    telegram_username: str | None,
) -> PairingOutcome:
    """Consume one Platform pairing code.

    Parameters
    ----------
    client : httpx.AsyncClient
        Dedicated Platform client.
    config : BotConfig
        Runtime configuration containing the Platform origin.
    token : str
        One-time pairing code.
    chat_id : int
        Telegram private-chat identifier.
    telegram_username : str or None
        Optional Telegram username.

    Returns
    -------
    PairingOutcome
        Atomic pairing outcome.

    Raises
    ------
    httpx.HTTPStatusError
        If Platform rejects the request outside the stable outcome contract.
    """
    response = await client.post(
        f"{config.platform_api_url}/internal/telegram/pairings",
        json={
            "token": token,
            "chat_id": chat_id,
            "telegram_username": telegram_username,
        },
    )
    response.raise_for_status()
    payload = response.json()
    outcome = payload.get("outcome")
    if outcome not in {"expired", "invalid", "paired", "used"}:
        raise RuntimeError("Platform returned an invalid pairing outcome")
    return PairingOutcome(
        outcome=outcome,
        user_id=_optional_int(payload.get("user_id")),
        prior_chat_id=_optional_int(payload.get("prior_chat_id")),
    )


async def unpair_chat(
    client: httpx.AsyncClient,
    config: BotConfig,
    chat_id: int,
) -> bool:
    """Remove one chat pairing idempotently.

    Parameters
    ----------
    client : httpx.AsyncClient
        Dedicated Platform client.
    config : BotConfig
        Runtime configuration containing the Platform origin.
    chat_id : int
        Telegram private-chat identifier.

    Returns
    -------
    bool
        Whether an active pairing was removed.

    Raises
    ------
    httpx.HTTPStatusError
        If Platform rejects or cannot serve the request.
    """
    response = await client.delete(
        f"{config.platform_api_url}/internal/telegram/pairings/{chat_id}"
    )
    response.raise_for_status()
    removed = response.json().get("removed")
    if not isinstance(removed, bool):
        raise RuntimeError("Platform returned an invalid unpair result")
    return removed


async def record_event(  # noqa: PLR0913 - bounded semantic event fields stay explicit
    client: httpx.AsyncClient,
    config: BotConfig,
    *,
    level: Literal["debug", "info", "warning", "error", "critical"],
    category: Literal["error", "job", "source", "auth", "config"],
    message: str,
    context: dict[str, object] | None = None,
    correlation_id: uuid.UUID | None = None,
) -> None:
    """Persist one Telegram semantic event through Platform.

    Event delivery is best-effort: an outage is logged locally and never blocks
    the originating bot command.

    Parameters
    ----------
    client : httpx.AsyncClient
        Dedicated Platform client.
    config : BotConfig
        Runtime configuration containing the Platform origin.
    level : {"debug", "info", "warning", "error", "critical"}
        Event severity.
    category : {"error", "job", "source", "auth", "config"}
        Semantic category.
    message : str
        Short event identifier.
    context : dict[str, object] or None, optional
        JSON-compatible context.
    correlation_id : UUID or None, optional
        Request correlation identifier.
    """
    try:
        response = await client.post(
            f"{config.platform_api_url}/internal/telegram/events",
            json={
                "level": level,
                "category": category,
                "message": message,
                "context": context or {},
                "correlation_id": str(correlation_id) if correlation_id else None,
            },
        )
        response.raise_for_status()
    except (httpx.HTTPError, RuntimeError):
        logger.warning("Platform event delivery failed", exc_info=True)


def _parse_pairing(payload: object) -> UserPairing:
    if not isinstance(payload, dict):
        raise RuntimeError("Platform returned an invalid pairing record")
    user_id = payload.get("user_id")
    chat_id = payload.get("chat_id")
    username = payload.get("telegram_username")
    paired_at = payload.get("paired_at")
    if (
        not isinstance(user_id, int)
        or isinstance(user_id, bool)
        or not isinstance(chat_id, int)
        or isinstance(chat_id, bool)
        or (username is not None and not isinstance(username, str))
        or (paired_at is not None and not isinstance(paired_at, str))
    ):
        raise RuntimeError("Platform returned an invalid pairing record")
    return UserPairing(user_id, chat_id, username, paired_at)


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


__all__ = [
    "PairingOutcome",
    "TelegramRuntime",
    "TimerPreferences",
    "UserPairing",
    "get_runtime_context",
    "get_timer_preferences",
    "list_user_pairings",
    "pair_chat",
    "record_event",
    "resolve_pairing",
    "unpair_chat",
]
