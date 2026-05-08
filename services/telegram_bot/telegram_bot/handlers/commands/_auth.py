"""Shared auth decorator and correlation-id middleware for command handlers."""

from __future__ import annotations

import asyncio
import logging
import uuid
from functools import wraps
from typing import Any

from jarvis_common.event_log import log_event
from jarvis_common.logging_config import correlation_id_var
from telegram import Update
from telegram.ext import ContextTypes

from telegram_bot.handlers.helpers import auth_check, get_config, get_db

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Correlation-id + first-chat auth-event tracking
# ---------------------------------------------------------------------------

_SEEN_CHATS: set[int] = set()
_SEEN_LOCK = asyncio.Lock()


async def _maybe_emit_auth_event(chat_id: int, pool: Any) -> None:
    """Emit a one-time ``auth`` event the first time a chat sends a command.

    Uses an in-memory set guarded by an asyncio.Lock.  The set is not
    persisted — a bot restart resets it.  That is intentional for this
    single-user pre-launch deployment.
    """
    async with _SEEN_LOCK:
        if chat_id in _SEEN_CHATS:
            return
        _SEEN_CHATS.add(chat_id)
    await log_event(
        pool=pool,
        level="info",
        category="auth",
        source="telegram_bot",
        message="chat_active",
        context={"chat_id": chat_id},
    )


def auth_required(func: Any) -> Any:
    """Decorator that:

    1. Sets a fresh ``correlation_id`` ContextVar for each invocation.
    2. Emits a one-time ``auth`` event the first time a chat is seen.
    3. Rejects messages from unauthorised chats.
    """

    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Any:
        # --- correlation id ---
        corr = uuid.uuid4()
        token = correlation_id_var.set(corr)
        try:
            config = get_config(context)
            db_pool = get_db(context)

            # --- first-chat auth event ---
            if update.effective_chat is not None:
                await _maybe_emit_auth_event(update.effective_chat.id, db_pool)

            # --- auth gate ---
            if not await auth_check(update, config, db_pool):
                chat_id = update.effective_chat.id if update.effective_chat else "unknown"
                logger.warning(
                    "Unauthorised access attempt from chat_id=%s",
                    chat_id,
                )
                return
            return await func(update, context)
        finally:
            correlation_id_var.reset(token)

    return wrapper
