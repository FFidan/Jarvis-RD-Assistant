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

# Upper bound on any sliding-window/cooldown horizon a handler configures.
# A key idle beyond this can never affect a future rate decision, so it is
# safe to evict. Generous so legitimate long cooldowns (e.g. /pulse_now) keep
# their state.
_MAX_HORIZON_SECONDS = 3600


def _evict_idle_keys(now: float, active_key: str) -> None:
    """Evict keys with no recent activity to bound dict growth (TG-SEC-01).

    The GC prunes stale timestamps in place but never removed the now-empty
    keys, so ``_timestamps`` grew one entry per unique ``chat:command`` forever
    (one-off / long-idle chats leak memory). This opportunistic sweep, run on
    each invocation, drops keys whose every stamp has aged beyond the longest
    window any decorator could care about.

    *active_key* (the caller currently inside its own lock, about to append a
    fresh stamp) is skipped.  Synchronous (no ``await``), so the scan is
    atomic w.r.t. other coroutines.

    ``_locks`` is intentionally NOT evicted here.  A coroutine that has just
    released its lock but whose waiter hasn't been scheduled yet would have its
    lock object deleted; the next caller would then create a brand-new lock
    (defaultdict) for the same key, bypassing the rate limiter for an entire
    window (M12a — woken-waiter window).  Lock entries are tiny asyncio.Lock
    objects; the key space is bounded by the finite set of distinct
    ``chat_id:command`` pairs, so the omission is safe.
    """
    horizon = _MAX_HORIZON_SECONDS
    for key in [k for k in _timestamps if k != active_key]:
        stamps = _timestamps[key]
        if stamps and now - stamps[-1] < horizon:
            continue  # has activity within any plausible window — keep
        del _timestamps[key]


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

            # Decide allow/deny under the lock; perform the user-facing reply
            # OUTSIDE the lock so same-key callers aren't serialized across the
            # network round-trip. ``denial`` is None when the call is allowed.
            denial: str | None = None
            async with _locks[key]:
                now = time.monotonic()
                horizon = max(window_seconds, cooldown_seconds)

                # Garbage-collect old entries unconditionally so stale timestamps
                # can never skew the window for long-idle users.
                stamps = _timestamps[key]
                stamps[:] = [t for t in stamps if now - t < horizon]

                # --- cooldown check (heavy commands) ---
                if cooldown_seconds and stamps and (now - stamps[-1]) < cooldown_seconds:
                    remaining = int(cooldown_seconds - (now - stamps[-1]))
                    logger.warning(
                        "Rate-limited %s (cooldown %ds remaining) chat=%s",
                        func.__name__,
                        remaining,
                        chat_id,
                    )
                    denial = f"Please wait {remaining}s before using this command again."
                else:
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
                        denial = (
                            f"Rate limit exceeded — max {max_calls} calls per {window_seconds}s."
                        )
                    else:
                        stamps.append(now)
                        # TG-SEC-01: bound dict growth by evicting long-idle keys.
                        _evict_idle_keys(now, key)

            if denial is not None:
                if update.message:
                    await update.message.reply_text(denial)
                elif update.callback_query:
                    await update.callback_query.answer(
                        text="Rate limit exceeded — try again later", show_alert=True
                    )
                return None

            return await func(update, context)

        return wrapper

    return decorator
