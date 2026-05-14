"""Shared helper functions for Telegram bot handlers.

Provides common utilities for accessing bot configuration, database pool,
HTTP client, and authorisation checks.
"""

from __future__ import annotations

import logging

import asyncpg
import httpx
from telegram import Update
from telegram.ext import ContextTypes

from telegram_bot.config import BotConfig

logger = logging.getLogger(__name__)


def get_config(context: ContextTypes.DEFAULT_TYPE) -> BotConfig:
    """Return the shared ``BotConfig`` instance.

    Thin accessor kept as a named function (rather than inlined dict lookup)
    so that tests can patch ``helpers._get_config`` in one place instead of
    mocking ``context.application.bot_data`` at every call site.
    """
    return context.application.bot_data["config"]


def get_db(context: ContextTypes.DEFAULT_TYPE) -> asyncpg.Pool:
    """Return the shared asyncpg connection pool.

    Thin accessor kept as a named function for mockability in tests.
    """
    return context.application.bot_data["db_pool"]


def get_http(context: ContextTypes.DEFAULT_TYPE) -> httpx.AsyncClient:
    """Return the shared httpx async client.

    Thin accessor kept as a named function for mockability in tests.
    """
    return context.application.bot_data["http_client"]


async def auth_check(
    update: Update,
    config: BotConfig,
    db_pool: asyncpg.Pool,
) -> bool:
    """Check whether the incoming chat is authorised.

    Priority order:
    1. ``TELEGRAM_CHAT_ID`` env var (via ``config.telegram_chat_id``) — if set
       and matches, allow immediately.
    2. DB fallback: ``user_config.telegram.owner_chat_id`` (populated by the
       dashboard pairing flow). asyncpg's JSONB codec decodes the value, which
       may be ``None``, ``int``, or ``str``.
    3. Multi-tenant pairing: ``telegram_user_pairings.chat_id`` (migration 071).
       Any chat_id present in this table was explicitly paired by a registered
       user and is therefore authorised.
    """
    chat = update.effective_chat
    if chat is None:
        return False
    env_chat_id = getattr(config, "telegram_chat_id", None)
    if env_chat_id and chat.id == env_chat_id:
        return True
    try:
        row = await db_pool.fetchval(
            "SELECT value FROM user_config WHERE key = 'telegram.owner_chat_id' AND user_id IS NULL"
        )
    except Exception:
        logger.warning("auth_check: DB error reading owner_chat_id; denying request", exc_info=True)
        return False
    if row is not None:
        try:
            return chat.id == int(row)
        except (ValueError, TypeError):
            pass
    try:
        pairing_row = await db_pool.fetchrow(
            "SELECT user_id FROM telegram_user_pairings WHERE chat_id = $1",
            chat.id,
        )
    except Exception:
        logger.warning(
            "auth_check: DB error reading telegram_user_pairings; denying request", exc_info=True
        )
        return False
    return pairing_row is not None
