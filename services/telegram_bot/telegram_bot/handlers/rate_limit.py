"""Per-command rate limiting for Telegram bot handlers.

Prevents abuse (DoS) if a Telegram session is hijacked.
Uses an in-memory sliding-window approach — no external dependencies.
"""

from __future__ import annotations

import asyncio
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

# Per-key asyncio locks — lazy creation via defaultdict.
# Guards the read-check-then-append sequence against concurrent coroutines
# (TOCTOU: two coroutines both pass `len(recent) >= max_calls` before either
# appends, allowing max_calls+N invocations to slip through).
_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


def rate_limit(
    max_calls: int = 5,
    window_seconds: int = 60,
    cooldown_seconds: int = 0,
):
    """Decorator factory that rate-limits a Telegram command handler.

    *cooldown_seconds* > 0 enforces a hard per-command cooldown after the last
    invocation (distinct from the sliding-window cap). Use for heavyweight
    commands like ``/pulse_now``.

    Thread/coroutine safety: all timestamp mutations happen under a per-key
    ``asyncio.Lock`` so that concurrent invocations cannot interleave between
    the window check and the append (TOCTOU fix, DOM-D-06).

    Parameters
    ----------
    max_calls : int
        Maximum number of invocations allowed within *window_seconds*.
    window_seconds : int
        Sliding-window size in seconds.
    cooldown_seconds : int
        Minimum wait between any two successive calls (0 = disabled). When
        non-zero, the cooldown applies *in addition* to the sliding-window cap
        and uses the same underlying timestamp store.

    Returns
    -------
    Callable
        A decorator that wraps the handler function with rate-limiting logic.
        Blocked calls reply with a user-facing message and return ``None``
        without invoking the wrapped handler.
    """

    def decorator(func):  # type: ignore[no-untyped-def]
        @wraps(func)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Any:
            if update.effective_chat is None:
                # Anonymous update — no chat to scope a bucket to; bypass the
                # rate limiter so all anonymous traffic doesn't share one slot.
                return await func(update, context)
            chat_id = str(update.effective_chat.id)
            key = f"{chat_id}:{func.__module__}.{func.__qualname__}"

            async with _locks[key]:
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
                        elif update.callback_query:
                            await update.callback_query.answer(
                                text="Rate limit exceeded — try again later", show_alert=True
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
                    elif update.callback_query:
                        await update.callback_query.answer(
                            text="Rate limit exceeded — try again later", show_alert=True
                        )
                    return None

                stamps.append(now)

            return await func(update, context)

        return wrapper

    return decorator
