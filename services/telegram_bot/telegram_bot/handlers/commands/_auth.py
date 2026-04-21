"""Shared auth decorator for command handlers."""

from __future__ import annotations

import logging
from functools import wraps
from typing import Any

from telegram import Update
from telegram.ext import ContextTypes

from telegram_bot.handlers.helpers import _auth_check, _get_config, _get_db

logger = logging.getLogger(__name__)


def auth_required(func: Any) -> Any:
    """Decorator that rejects messages from unauthorised chats."""

    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Any:
        config = _get_config(context)
        db_pool = _get_db(context)
        if not await _auth_check(update, config, db_pool):
            chat_id = update.effective_chat.id if update.effective_chat else "unknown"
            logger.warning(
                "Unauthorised access attempt from chat_id=%s",
                chat_id,
            )
            return
        return await func(update, context)

    return wrapper
