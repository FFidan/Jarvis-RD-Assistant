"""Shared helper functions for Telegram bot handlers.

Provides common utilities for accessing bot configuration, database pool,
HTTP client, and authorisation checks.
"""

from __future__ import annotations

import logging

import asyncpg
import httpx
from telegram import Update
from telegram.constants import ChatType
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


def get_jarvis_user_id(context: ContextTypes.DEFAULT_TYPE) -> int | None:
    """Return the JARVIS user-id cached by ``auth_required`` in ``context.user_data``.

    Returns ``None`` only when ``context.user_data`` is unavailable; for an
    authorised (paired) chat the decorator always stashes a real integer id.
    """
    return context.user_data.get("jarvis_user_id") if context.user_data is not None else None


async def auth_check(
    update: Update,
    config: BotConfig,
    db_pool: asyncpg.Pool,
) -> tuple[bool, int | None]:
    """Check whether the incoming chat is paired and return its user_id.

    Pairing (``telegram_user_pairings``) is the sole bot-identity mechanism;
    there is no legacy env-var / dashboard-owner path.

    Returns
    -------
    tuple[bool, int | None]
        ``(True, user_id)`` when the chat is paired — the user_id is the DB PK
        from ``telegram_user_pairings`` and MUST be used to scope all per-user
        queries downstream.
        ``(False, None)`` when unauthorised (no chat, DB error, or no pairing
        row for this chat_id).

    Invariant: ``authorized is True`` ⟺ ``user_id is not None``.

    The *config* parameter is retained for call-site stability but is no longer
    consulted by this function.
    """
    chat = update.effective_chat
    if chat is None:
        return False, None
    # Identity is bound to chat_id, so a group/supergroup pairing would let every
    # member act as the paired user. Only 1:1 private chats may hold an identity.
    if chat.type != ChatType.PRIVATE:
        return False, None
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
