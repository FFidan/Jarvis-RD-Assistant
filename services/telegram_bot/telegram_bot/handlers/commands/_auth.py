"""Shared auth decorator and correlation-id middleware for command handlers."""

from __future__ import annotations

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


def auth_required(func: Any) -> Any:
    """Decorator that enforces authentication on a Telegram command handler.

    Specifically:

    1. Sets a fresh ``correlation_id`` ContextVar for each invocation.
    2. Emits a one-time ``auth`` event the first time a chat is seen (tracked
       via ``context.user_data["_auth_seen"]`` so the flag is per-chat session
       and does not leak across chats via a shared module-level set).
    3. Rejects messages from unauthorised chats.
    4. Rejects authorized chats that have no paired JARVIS user account,
       prompting them to complete the pairing flow via the dashboard.

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
            db_pool = get_db(context)

            # --- first-chat auth event (per-session, not module-global) ---
            if update.effective_chat is not None and context.user_data is not None:
                if not context.user_data.get("_auth_seen"):
                    context.user_data["_auth_seen"] = True
                    await log_event(
                        pool=db_pool,
                        level="info",
                        category="auth",
                        source="telegram_bot",
                        message="chat_active",
                        context={"chat_id": update.effective_chat.id},
                    )

            # --- auth gate ---
            authorized, jarvis_user_id = await auth_check(update, config, db_pool)
            if not authorized:
                chat_id = update.effective_chat.id if update.effective_chat else "unknown"
                logger.warning(
                    "Unauthorised access attempt from chat_id=%s",
                    chat_id,
                )
                return
            if jarvis_user_id is None:
                # Authorized chat (env-var or legacy owner_chat_id) but no paired
                # JARVIS account.  In multi-user mode every command requires a
                # pairing — fail loudly so the user knows what to do.
                logger.warning(
                    "Authorized chat has no paired JARVIS user; blocking command chat_id=%s",
                    update.effective_chat.id if update.effective_chat else "unknown",
                )
                if update.message is not None:
                    await update.message.reply_text(
                        "⚠️ Your Telegram account is not yet linked to a JARVIS user.\n\n"
                        "To pair, open the JARVIS dashboard → <b>Settings → Integrations</b> "
                        "and follow the Telegram pairing steps. "
                        "You will receive a deep-link that completes the pairing automatically.",
                        parse_mode="HTML",
                    )
                return
            if context.user_data is not None:
                context.user_data["jarvis_user_id"] = jarvis_user_id
            return await func(update, context)
        finally:
            correlation_id_var.reset(token)

    return wrapper
