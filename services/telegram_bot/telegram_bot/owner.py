"""Resolve Telegram chat IDs for outbound (scheduled/push) messages.

:func:`list_user_pairings` returns all ``(user_id, chat_id)`` rows from the
``telegram_user_pairings`` table so orchestrators can iterate and deliver
per-user content.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import asyncpg

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
