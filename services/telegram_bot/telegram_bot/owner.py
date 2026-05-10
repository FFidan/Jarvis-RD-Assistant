"""Resolve the owner chat ID for outbound (scheduled/push) Telegram messages.

The owner chat ID can come from two sources, checked in priority order:
1. ``config.telegram_chat_id`` — set via the ``TELEGRAM_CHAT_ID`` env var
   (backward-compatible, classic single-user mode).
2. ``user_config`` DB row ``telegram.owner_chat_id`` — populated by the
   dashboard pairing flow (new default for fresh installs).

All outbound schedulers must call :func:`resolve_owner_chat_id` and skip
delivery when it returns ``None`` to avoid silently discarding messages.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import asyncpg

if TYPE_CHECKING:
    from .config import BotConfig

logger = logging.getLogger(__name__)


async def resolve_owner_chat_id(db_pool: asyncpg.Pool, config: BotConfig) -> int | None:
    """Return the chat ID to use for scheduled/outbound messages.

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
            "SELECT value FROM user_config WHERE key = 'telegram.owner_chat_id'"
        )
    except Exception:
        logger.exception("DB error resolving telegram.owner_chat_id")
        return None

    if row is None or row == "null":
        return None
    try:
        return int(row)
    except (ValueError, TypeError):
        logger.warning("telegram.owner_chat_id value %r is not a valid integer", row)
        return None
