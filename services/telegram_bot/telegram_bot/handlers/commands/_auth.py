"""Shared auth decorator and correlation-id middleware for command handlers."""

from __future__ import annotations

import logging
import uuid
from functools import wraps
from typing import Any

from jarvis_common.logging_config import correlation_id_var
from telegram import Update
from telegram.ext import ContextTypes

from telegram_bot.handlers.helpers import auth_check, get_config, get_platform_http
from telegram_bot.platform_client import record_event

logger = logging.getLogger(__name__)


def auth_required(func: Any) -> Any:
    """Decorator that enforces authentication on a Telegram command handler.

    Specifically:

    1. Sets a fresh ``correlation_id`` ContextVar for each invocation.
    2. Emits a one-time ``auth`` event the first time a chat is seen (tracked
       via ``context.user_data["_auth_seen"]`` so the flag is per-chat session
       and does not leak across chats via a shared module-level set).
    3. Rejects messages from unpaired chats, prompting them to complete the
       pairing flow via the dashboard + ``/pair``.

    Parameters
    ----------
    func : Callable
        Async Telegram handler ``(update, context) -> Any``.

    Returns
    -------
    Callable
        Wrapped handler with auth + correlation-id middleware applied.
    """

    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Any:
        # --- correlation id ---
        corr = uuid.uuid4()
        token = correlation_id_var.set(corr)
        try:
            config = get_config(context)
            platform_client = get_platform_http(context)

            # --- first-chat auth event (per-session, not module-global) ---
            if update.effective_chat is not None and context.user_data is not None:
                if not context.user_data.get("_auth_seen"):
                    context.user_data["_auth_seen"] = True
                    await record_event(
                        platform_client,
                        config,
                        level="info",
                        category="auth",
                        message="chat_active",
                        context={"chat_id": update.effective_chat.id},
                    )

            # --- auth gate (pairing is the sole identity mechanism) ---
            authorized, jarvis_user_id = await auth_check(update, config, platform_client)
            if not authorized:
                chat_id = update.effective_chat.id if update.effective_chat else "unknown"
                logger.warning(
                    "Unauthorised access attempt from chat_id=%s",
                    chat_id,
                )
                if update.message is not None:
                    await update.message.reply_text(
                        "🔗 Link your JARVIS account first: open the dashboard → "
                        "Settings → Integrations → Telegram, then run /pair <code>."
                    )
                return
            if context.user_data is not None:
                context.user_data["jarvis_user_id"] = jarvis_user_id
            return await func(update, context)
        finally:
            correlation_id_var.reset(token)

    return wrapper
