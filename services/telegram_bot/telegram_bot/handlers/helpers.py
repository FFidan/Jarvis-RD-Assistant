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


def _owner_headers(config: BotConfig, user_id: int | None) -> dict[str, str]:
    """Build the standard backend auth headers for a bot→backend HTTP call.

    Always includes ``X-API-Key`` when configured. Adds ``X-Owner-User-Id``
    when *user_id* is not ``None`` so the backend can scope the response to
    the correct paired user.
    """
    headers: dict[str, str] = {}
    if config.jarvis_api_key:
        headers["X-API-Key"] = config.jarvis_api_key.get_secret_value()
    if user_id is not None:
        headers["X-Owner-User-Id"] = str(user_id)
    return headers


async def auth_check(
    update: Update,
    config: BotConfig,
    db_pool: asyncpg.Pool,
) -> tuple[bool, int | None]:
    """Check whether the incoming chat is authorised and return its user_id.

    Returns
    -------
    tuple[bool, int | None]
        ``(True, user_id)`` for paired multi-tenant chats — the user_id is the
        DB PK from ``telegram_user_pairings`` and MUST be used to scope all
        per-user queries downstream.
        ``(True, None)`` for the legacy single-tenant owner paths (env-var
        match or ``user_config.telegram.owner_chat_id``) — downstream queries
        stay unscoped to preserve owner visibility.
        ``(False, None)`` for unauthorised chats.

    Priority order:
    1. ``TELEGRAM_CHAT_ID`` env var (via ``config.telegram_chat_id``).
    2. DB fallback: ``user_config.telegram.owner_chat_id`` (dashboard pairing).
    3. Multi-tenant pairing: ``telegram_user_pairings.chat_id`` (migration 071).
    """
    chat = update.effective_chat
    if chat is None:
        return False, None
    env_chat_id = getattr(config, "telegram_chat_id", None)
    if env_chat_id and chat.id == env_chat_id:
        return True, None
    try:
        row = await db_pool.fetchval(
            "SELECT value FROM user_config WHERE key = 'telegram.owner_chat_id' AND user_id IS NULL"
        )
    except Exception:
        logger.warning("auth_check: DB error reading owner_chat_id; denying request", exc_info=True)
        return False, None
    if row is not None:
        try:
            if chat.id == int(row):
                return True, None
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
        return False, None
    if pairing_row is None:
        return False, None
    return True, pairing_row["user_id"]
