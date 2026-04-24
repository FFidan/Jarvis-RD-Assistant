"""Per-command rate limiting for Telegram bot handlers.

Prevents abuse (DoS) if a Telegram session is hijacked.
Uses an in-memory sliding-window approach — no external dependencies.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from functools import wraps
from typing import Any

from telegram import Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

# chat_id:command_name → list of monotonic timestamps
_timestamps: dict[str, list[float]] = defaultdict(list)


def rate_limit(
    max_calls: int = 5,
    window_seconds: int = 60,
    cooldown_seconds: int = 0,
):
    """Decorator that rate-limits a Telegram command handler.

    *cooldown_seconds* > 0 enforces a hard per-command cooldown after the last
    invocation (distinct from the sliding-window cap). Use for heavyweight
    commands like ``/pulse_now``.
    """

    def decorator(func):  # type: ignore[no-untyped-def]
        @wraps(func)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Any:
            chat_id = str(update.effective_chat.id if update.effective_chat else "unknown")
            key = f"{chat_id}:{func.__name__}"
            now = time.monotonic()
            horizon = max(window_seconds, cooldown_seconds)

            # Garbage-collect old entries unconditionally so stale timestamps
            # can never skew the window for long-idle users.
            stamps = _timestamps[key]
            stamps[:] = [t for t in stamps if now - t < horizon]

            # --- cooldown check (heavy commands) ---
            if cooldown_seconds and stamps:
                elapsed = now - stamps[-1]
                if elapsed < cooldown_seconds:
                    remaining = int(cooldown_seconds - elapsed)
                    logger.warning(
                        "Rate-limited %s (cooldown %ds remaining) chat=%s",
                        func.__name__,
                        remaining,
                        chat_id,
                    )
                    if update.message:
                        await update.message.reply_text(
                            f"Please wait {remaining}s before using this command again."
                        )
                    return None

            # --- sliding window check ---
            recent = [t for t in stamps if now - t < window_seconds]
            if len(recent) >= max_calls:
                logger.warning(
                    "Rate-limited %s (%d/%d in %ds) chat=%s",
                    func.__name__,
                    len(recent),
                    max_calls,
                    window_seconds,
                    chat_id,
                )
                if update.message:
                    await update.message.reply_text(
                        f"Rate limit exceeded — max {max_calls} calls per {window_seconds}s."
                    )
                return None

            stamps.append(now)
            return await func(update, context)

        return wrapper

    return decorator
