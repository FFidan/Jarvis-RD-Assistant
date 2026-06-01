"""Resolve Telegram chat IDs for outbound (scheduled/push) messages.

Two pairing modes co-exist:

1. **Legacy single-tenant** — ``TELEGRAM_CHAT_ID`` env var OR
   ``user_config.telegram.owner_chat_id`` row. Used by
   :func:`resolve_owner_chat_id` which all existing schedulers call.

2. **Multi-tenant per-user** — ``telegram_user_pairings`` table (added in
   migration 071). :func:`list_user_pairings` returns all (user_id, chat_id)
   rows so orchestrators can iterate and deliver per-user content.

Orchestrators that have access to a DB pool use :func:`list_user_pairings`.
They fall back to :func:`resolve_owner_chat_id` for the legacy single-tenant
path only when no rows exist in the new table.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

import asyncpg

if TYPE_CHECKING:
    from .config import BotConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UserPairing:
    """A resolved (user_id, chat_id) pairing from ``telegram_user_pairings``."""

    user_id: int
    chat_id: int
    telegram_username: str | None = None


async def list_user_pairings(db_pool: asyncpg.Pool) -> list[UserPairing]:
    """Return all active per-user pairings from ``telegram_user_pairings``.

    Returns an empty list on DB error (callers log and skip delivery).

    Parameters
    ----------
    db_pool:
        Database connection pool.

    Returns
    -------
    list[UserPairing]
        One entry per paired user. May be empty.
    """
    try:
        rows = await db_pool.fetch(
            "SELECT user_id, chat_id, telegram_username FROM telegram_user_pairings"
        )
        return [
            UserPairing(
                user_id=int(r["user_id"]),
                chat_id=int(r["chat_id"]),
                telegram_username=r["telegram_username"],
            )
            for r in rows
        ]
    except Exception:
        logger.exception("DB error listing telegram_user_pairings")
        return []


async def resolve_owner_chat_id(db_pool: asyncpg.Pool, config: BotConfig) -> int | None:
    """Return the chat ID to use for scheduled/outbound messages.

    Legacy single-tenant helper.  Multi-tenant orchestrators should use
    :func:`list_user_pairings` instead.

    Parameters
    ----------
    db_pool:
        Database connection pool used to query ``user_config`` when the env
        var is not set.
    config:
        Bot configuration.  ``config.telegram_chat_id`` is checked first.

    Returns
    -------
    int | None
        The resolved owner chat ID, or ``None`` if neither source has a value.
        Callers **must** handle the ``None`` case (log + skip delivery).
    """
    if config.telegram_chat_id is not None:
        return config.telegram_chat_id

    try:
        row = await db_pool.fetchval(
            "SELECT value FROM user_config WHERE key = 'telegram.owner_chat_id' AND user_id IS NULL"
        )
    except Exception:
        logger.exception("DB error resolving telegram.owner_chat_id")
        return None

    if row is None:
        return None
    try:
        return int(row)
    except (ValueError, TypeError):
        logger.warning("telegram.owner_chat_id value %r is not a valid integer", row)
        return None
