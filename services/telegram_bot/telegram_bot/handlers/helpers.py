"""Shared helper functions for Telegram bot handlers.

Provides common utilities for accessing bot configuration, database pool,
HTTP client, and authorisation checks.
"""

from __future__ import annotations

import asyncpg
import httpx
from telegram import Update
from telegram.ext import ContextTypes

from telegram_bot.config import BotConfig


def _get_config(context: ContextTypes.DEFAULT_TYPE) -> BotConfig:
    """Return the shared ``BotConfig`` instance.

    Thin accessor kept as a named function (rather than inlined dict lookup)
    so that tests can patch ``helpers._get_config`` in one place instead of
    mocking ``context.application.bot_data`` at every call site.
    """
    return context.application.bot_data["config"]


def _get_db(context: ContextTypes.DEFAULT_TYPE) -> asyncpg.Pool:
    """Return the shared asyncpg connection pool.

    Thin accessor kept as a named function for mockability in tests.
    """
    return context.application.bot_data["db_pool"]


def _get_http(context: ContextTypes.DEFAULT_TYPE) -> httpx.AsyncClient:
    """Return the shared httpx async client.

    Thin accessor kept as a named function for mockability in tests.
    """
    return context.application.bot_data["http_client"]


async def _auth_check(
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
    """
    chat = update.effective_chat
    if chat is None:
        return False
    env_chat_id = getattr(config, "telegram_chat_id", None)
    if env_chat_id and chat.id == env_chat_id:
        return True
    try:
        row = await db_pool.fetchval(
            "SELECT value FROM user_config WHERE key = 'telegram.owner_chat_id'"
        )
    except Exception:
        return False
    if row is None:
        return False
    try:
        return chat.id == int(row)
    except (ValueError, TypeError):
        return False
