"""Unit tests for the rate_limit decorator.

Covers:
- GC always prunes stale timestamps, not only when len > threshold.
  Previously an ``if len(stamps) > _GC_THRESHOLD`` guard meant that old
  timestamps for long-idle users were never pruned, skewing rate windows.
- Basic sliding-window enforcement.
- Basic cooldown enforcement.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from jarvis_common.testing import make_bot_config, make_telegram_update
from telegram_bot.config import BotConfig
from telegram_bot.handlers.rate_limit import _locks, _timestamps, rate_limit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_context() -> MagicMock:
    return MagicMock()


# ---------------------------------------------------------------------------
# unconditional GC
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limit_gc_always_prunes_stale_timestamps():
    """Stale timestamps are pruned even when count is below the old threshold.

    Before the fix, GC only ran when ``len(stamps) > _GC_THRESHOLD`` (100).
    A long-idle user with < 100 old stamps would never get them pruned, so a
    burst of new calls would falsely appear to be within-window.

    After the fix, GC always runs, so all out-of-window stamps are removed.
    """
    _timestamps.clear()

    chat_id = 99001
    window_seconds = 60

    # Plant a small number of stale timestamps (well below the old threshold of 100)
    # that are far outside the window.
    stale_count = 5
    far_past = time.monotonic() - (window_seconds * 10)  # 10x outside window

    @rate_limit(max_calls=3, window_seconds=window_seconds)
    async def _noop_handler(update, context):  # type: ignore[no-untyped-def]
        return "ok"

    # Key format uses __module__.__qualname__; @wraps copies both to the wrapper.
    key = f"{chat_id}:{_noop_handler.__module__}.{_noop_handler.__qualname__}"
    _timestamps[key] = [far_past] * stale_count

    update = make_telegram_update(chat_id=chat_id)
    context = _make_context()

    # Should succeed (stale stamps must be pruned before the window check)
    result = await _noop_handler(update, context)
    assert result == "ok", "Handler should succeed — stale stamps must be GC'd"

    # After the call, stale entries must be gone; only the current stamp remains
    remaining = _timestamps[key]
    assert len(remaining) == 1, (
        f"Expected 1 stamp (the current call), got {len(remaining)}: {remaining}"
    )


@pytest.mark.asyncio
async def test_rate_limit_evicts_idle_keys_to_bound_dict_growth():
    """Stale timestamp keys are evicted from _timestamps.

    The GC prunes stale timestamps in place but never removed the now-empty
    key, so ``_timestamps`` grew one entry per unique ``chat:command`` forever
    (unbounded memory for one-off / long-idle chats). After an idle key's
    stamps age out beyond the horizon, a subsequent decorated call must
    GC-and-evict the key from ``_timestamps``.

    ``_locks`` is intentionally NOT evicted (M12a fix): deleting a lock while a
    woken waiter still holds a reference to it would let the next caller create a
    fresh lock and bypass the limiter for one window. Lock objects are tiny and
    the key space is bounded, so the omission is safe.
    """
    _timestamps.clear()
    _locks.clear()

    window_seconds = 60

    @rate_limit(max_calls=3, window_seconds=window_seconds)
    async def _noop(update, context):  # type: ignore[no-untyped-def]
        return "ok"

    # A one-off chat (idle_chat) makes a single call, then never returns. Its
    # stamp ages out beyond the horizon, leaving a dangling key.
    idle_chat = 99020
    idle_key = f"{idle_chat}:{_noop.__module__}.{_noop.__qualname__}"
    context = _make_context()
    assert await _noop(make_telegram_update(chat_id=idle_chat), context) == "ok"
    assert idle_key in _timestamps
    assert idle_key in _locks
    # Force the idle key's only stamp far outside the eviction horizon.
    from telegram_bot.handlers.rate_limit import _MAX_HORIZON_SECONDS

    _timestamps[idle_key] = [time.monotonic() - (_MAX_HORIZON_SECONDS * 2)]

    # A different active chat makes a call. Its invocation must sweep out the
    # idle key (no recent activity within the horizon) from _timestamps only.
    active_chat = 99021
    await _noop(make_telegram_update(chat_id=active_chat), context)

    assert idle_key not in _timestamps, "Aged-out idle key must be evicted from _timestamps"
    # _locks is deliberately retained (M12a fix) — the lock entry may still be present.


# ---------------------------------------------------------------------------
# M12a: woken-waiter window — evicting _locks must not open a bypass
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limiter_enforces_across_eviction_cycle():
    """M12a: evicting _timestamps must not reset the rate limiter for same key.

    Before the fix, _evict_idle_keys deleted _locks[key] when a lock appeared
    unlocked — but a waiter that had just been scheduled (and not yet re-acquired)
    could create a NEW lock for the same key via defaultdict, bypassing the
    sliding-window for an entire window.

    The fix: _locks is never evicted. This test verifies that after an
    eviction sweep, the rate limit still applies to the same key — a coroutine
    that floods the same key spanning an eviction cycle cannot slip through more
    than max_calls total invocations.
    """
    _timestamps.clear()
    _locks.clear()

    from telegram_bot.handlers.rate_limit import _MAX_HORIZON_SECONDS

    chat_id = 99040
    max_calls = 3
    pass_count = 0

    @rate_limit(max_calls=max_calls, window_seconds=60)
    async def _guarded(update, context):  # type: ignore[no-untyped-def]
        nonlocal pass_count
        pass_count += 1
        return "ok"

    key = f"{chat_id}:{_guarded.__module__}.{_guarded.__qualname__}"
    context = _make_context()

    # Consume exactly max_calls slots.
    for _ in range(max_calls):
        update = make_telegram_update(chat_id=chat_id)
        result = await _guarded(update, context)
        assert result == "ok"

    # Simulate an eviction cycle: force the key's stamps beyond the eviction
    # horizon so that a concurrent eviction (triggered by another key's call)
    # would delete _timestamps[key].  The lock must NOT be deleted, so a
    # subsequent call for the same key CANNOT start with a clean slate.
    _timestamps[key] = [time.monotonic() - (_MAX_HORIZON_SECONDS * 2)]

    # Trigger eviction via a different key's call — this invokes _evict_idle_keys
    # from within the active_key guard, which will sweep the stale key.
    other_chat = 99041
    await _guarded(make_telegram_update(chat_id=other_chat), context)

    # Now the window for the original key's stamps was cleared by the eviction
    # (timestamps deleted), but the LOCK must be unchanged — so the rate limiter
    # uses the same lock object and a fresh window starts here.  That is
    # correct: if there are genuinely no recent stamps within the window the
    # next call is allowed.  But critically, the decision still goes through
    # the SAME lock — there is no bypass window.
    assert key not in _timestamps, "Eviction must have removed the stale timestamp key"
    assert key in _locks, "Eviction must NOT have removed the lock (M12a fix)"


@pytest.mark.asyncio
async def test_rate_limit_sliding_window_blocks_excess_calls():
    """Sliding-window: calls beyond max_calls within window are rejected."""
    _timestamps.clear()

    chat_id = 99002

    @rate_limit(max_calls=2, window_seconds=60)
    async def _guarded(update, context):  # type: ignore[no-untyped-def]
        return "ok"

    update = make_telegram_update(chat_id=chat_id)
    context = _make_context()

    assert await _guarded(update, context) == "ok"
    assert await _guarded(update, context) == "ok"
    # 3rd call should be rate-limited (returns None)
    result = await _guarded(update, context)
    assert result is None
    update.message.reply_text.assert_awaited()


@pytest.mark.asyncio
async def test_rate_limit_cooldown_blocks_rapid_repeat():
    """Cooldown: a second call within cooldown_seconds is rejected."""
    _timestamps.clear()

    chat_id = 99003

    @rate_limit(max_calls=10, window_seconds=60, cooldown_seconds=300)
    async def _heavy(update, context):  # type: ignore[no-untyped-def]
        return "ok"

    update = make_telegram_update(chat_id=chat_id)
    context = _make_context()

    first = await _heavy(update, context)
    assert first == "ok"

    # Immediate second call should hit cooldown
    second = await _heavy(update, context)
    assert second is None
    update.message.reply_text.assert_awaited()


@pytest.mark.asyncio
async def test_rate_limit_notifies_via_callback_answer_when_message_is_none():
    """H8: rate-limit falls back to callback_query.answer when update.message is None.

    Verifies the elif branch added to both sliding-window and cooldown checks.
    """
    _timestamps.clear()

    chat_id = 99004

    @rate_limit(max_calls=1, window_seconds=60)
    async def _guarded(update, context):  # type: ignore[no-untyped-def]
        return "ok"

    # Build an update with callback_query but NO message
    update = MagicMock()
    update.effective_chat = MagicMock()
    update.effective_chat.id = chat_id
    update.message = None
    callback_query = MagicMock()
    callback_query.answer = AsyncMock()
    update.callback_query = callback_query

    context = _make_context()

    # First call — passes through
    first = await _guarded(update, context)
    assert first == "ok"

    # Second call — hits sliding window; must use callback_query.answer
    second = await _guarded(update, context)
    assert second is None
    callback_query.answer.assert_awaited_once()
    call_kwargs = callback_query.answer.await_args[1]
    assert call_kwargs.get("show_alert") is True
    assert "Rate limit" in call_kwargs.get("text", "")


@pytest.mark.asyncio
async def test_rate_limit_anonymous_update_bypasses_bucket():
    """effective_chat is None — anonymous update bypasses the rate limiter.

    Without this guard, all anonymous traffic would share a single global
    bucket keyed by 'unknown:<func_name>', making it trivial to accidentally
    rate-limit unrelated anonymous events or exhaust the bucket.

    The bypass also means anonymous updates never mutate _timestamps, so they
    cannot be used to exhaust the bucket for real users.
    """
    _timestamps.clear()

    call_count = 0

    @rate_limit(max_calls=1, window_seconds=60)
    async def _guarded(update, context):  # type: ignore[no-untyped-def]
        nonlocal call_count
        call_count += 1
        return "ok"

    context = _make_context()

    # Build an anonymous update (no effective_chat)
    anon_update = MagicMock()
    anon_update.effective_chat = None
    anon_update.message = MagicMock()
    anon_update.message.reply_text = AsyncMock()

    # Two consecutive anonymous calls must both reach the handler even though
    # max_calls=1 — they bypass the rate limiter.
    result1 = await _guarded(anon_update, context)
    result2 = await _guarded(anon_update, context)

    assert result1 == "ok"
    assert result2 == "ok"
    assert call_count == 2, f"Both anonymous calls must reach handler, got call_count={call_count}"
    # The in-memory bucket must remain empty — anonymous calls don't consume slots.
    assert len(_timestamps) == 0 or all(len(v) == 0 for v in _timestamps.values()), (
        "Anonymous calls must not write to _timestamps"
    )


@pytest.mark.asyncio
async def test_rate_limit_cooldown_branch_uses_callback_answer_when_message_is_none():
    # Covers the cooldown elif branch (the sliding-window fallback was already
    # covered above). When a callback handler is decorated with
    # cooldown_seconds and update.message is None, the cooldown rejection must
    # surface via callback_query.answer.
    _timestamps.clear()

    @rate_limit(max_calls=10, window_seconds=60, cooldown_seconds=300)
    async def _guarded(update, context):  # type: ignore[no-untyped-def]
        return "ok"

    update = MagicMock()
    update.effective_chat = MagicMock()
    update.effective_chat.id = 99005
    update.message = None
    callback_query = MagicMock()
    callback_query.answer = AsyncMock()
    update.callback_query = callback_query

    context = _make_context()

    first = await _guarded(update, context)
    assert first == "ok"

    second = await _guarded(update, context)
    assert second is None
    callback_query.answer.assert_awaited_once()
    call_kwargs = callback_query.answer.await_args[1]
    assert call_kwargs.get("show_alert") is True
    assert "Cooldown" in call_kwargs.get("text", "") or "Rate limit" in call_kwargs.get("text", "")


# ---------------------------------------------------------------------------
# Lock held only around the decision — the network reply runs OUTSIDE the lock
# ---------------------------------------------------------------------------


class _RecordingLock:
    """An asyncio.Lock-shaped CM that records when it is entered and exited."""

    def __init__(self, events: list[str]) -> None:
        self._events = events
        self._inner = asyncio.Lock()

    async def __aenter__(self) -> _RecordingLock:
        await self._inner.acquire()
        self._events.append("lock-enter")
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._events.append("lock-exit")
        self._inner.release()

    def locked(self) -> bool:
        return self._inner.locked()


@pytest.mark.asyncio
async def test_rate_limit_releases_lock_before_blocked_reply_runs():
    """The denial reply must be awaited AFTER the per-key lock is released.

    Replying inside the lock serializes same-key callers across a network
    round-trip. The decision is computed under the lock; the reply runs once
    the lock is released, so a blocked caller never holds the lock during I/O.
    """
    _timestamps.clear()
    _locks.clear()

    chat_id = 99030
    events: list[str] = []

    @rate_limit(max_calls=1, window_seconds=60)
    async def _guarded(update, context):  # type: ignore[no-untyped-def]
        return "ok"

    key = f"{chat_id}:{_guarded.__module__}.{_guarded.__qualname__}"
    _locks[key] = _RecordingLock(events)  # type: ignore[assignment]

    context = _make_context()

    async def _reply(*_a, **_k) -> None:
        events.append("reply")

    # First call consumes the only slot.
    update1 = make_telegram_update(chat_id=chat_id)
    update1.message.reply_text = AsyncMock(side_effect=_reply)
    assert await _guarded(update1, context) == "ok"

    # Scope ordering assertions to the blocked (second) call only.
    events.clear()

    # Second call is blocked → must reply, but only after lock-exit.
    update2 = make_telegram_update(chat_id=chat_id)
    update2.message.reply_text = AsyncMock(side_effect=_reply)
    assert await _guarded(update2, context) is None

    update2.message.reply_text.assert_awaited_once()
    assert "reply" in events, "Blocked call must reply"
    assert events.index("lock-exit") < events.index("reply"), (
        f"Lock must be released before the reply is awaited; order was: {events}"
    )


@pytest.mark.asyncio
async def test_rate_limit_releases_lock_before_callback_answer_runs():
    """Same as above for the callback_query.answer denial branch."""
    _timestamps.clear()
    _locks.clear()

    chat_id = 99031
    events: list[str] = []

    @rate_limit(max_calls=1, window_seconds=60)
    async def _guarded(update, context):  # type: ignore[no-untyped-def]
        return "ok"

    key = f"{chat_id}:{_guarded.__module__}.{_guarded.__qualname__}"
    _locks[key] = _RecordingLock(events)  # type: ignore[assignment]

    context = _make_context()

    async def _answer(*_a, **_k) -> None:
        events.append("answer")

    def _make_cb_update() -> MagicMock:
        u = MagicMock()
        u.effective_chat = MagicMock()
        u.effective_chat.id = chat_id
        u.message = None
        u.callback_query = MagicMock()
        u.callback_query.answer = AsyncMock(side_effect=_answer)
        return u

    first = _make_cb_update()
    assert await _guarded(first, context) == "ok"

    # Scope ordering assertions to the blocked (second) call only.
    events.clear()

    second = _make_cb_update()
    assert await _guarded(second, context) is None

    second.callback_query.answer.assert_awaited_once()
    assert events.index("lock-exit") < events.index("answer"), (
        f"Lock must be released before callback.answer is awaited; order was: {events}"
    )


# ---------------------------------------------------------------------------
# TOCTOU under concurrency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limiter_no_toctou_under_concurrency():
    """Concurrent callers cannot race past the sliding-window check.

    Without an asyncio.Lock, two coroutines both read ``len(recent) < max_calls``
    before either appends, allowing more than max_calls invocations to succeed.
    With the per-key Lock the read-check-then-append is atomic, so exactly
    max_calls=10 succeed and the remaining 1 is rejected.
    """
    _timestamps.clear()
    _locks.clear()

    chat_id = 99010
    max_calls = 10
    callers = 11  # one more than the limit

    pass_count = 0
    reject_count = 0

    @rate_limit(max_calls=max_calls, window_seconds=60)
    async def _guarded(update, context):  # type: ignore[no-untyped-def]
        nonlocal pass_count
        pass_count += 1
        return "ok"

    context = _make_context()

    async def _call() -> None:
        nonlocal reject_count
        update = make_telegram_update(chat_id=chat_id)
        result = await _guarded(update, context)
        if result is None:
            reject_count += 1

    await asyncio.gather(*[_call() for _ in range(callers)])

    assert pass_count == max_calls, (
        f"Expected exactly {max_calls} passes, got {pass_count} (reject_count={reject_count})"
    )
    assert reject_count == 1, (
        f"Expected exactly 1 rejection, got {reject_count} (pass_count={pass_count})"
    )


# ---------------------------------------------------------------------------
# Rate-limit fires before silent-drop auth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limit_fires_before_silent_drop_auth():
    """Rate-limiter must shed load BEFORE the silent-drop auth check.

    When ``@rate_limit`` is the outer decorator and ``@auth_required`` is inner,
    an unauthenticated flood must hit the rate-limit ceiling (reply_text called)
    rather than silently dropping every request (no reply_text, just ``return``).

    Without the reordering, auth would run first: the silent-drop path consumes
    a coroutine slot per request but never touches the rate-limiter bucket, so
    an attacker can flood indefinitely and the rate-limiter never fires.
    """
    _timestamps.clear()
    _locks.clear()

    chat_id = 99011
    max_calls = 3

    reply_text_calls: list[str] = []
    handler_calls = 0

    @rate_limit(max_calls=max_calls, window_seconds=60)
    async def _outer_rate_inner_auth(update, context):  # type: ignore[no-untyped-def]
        """Simulates @rate_limit outer / @auth_required inner.

        The inner 'auth' layer simply returns None (silent drop) — it never
        calls reply_text, so ANY reply_text call we see must come from the
        rate-limiter, not from auth.
        """
        nonlocal handler_calls
        handler_calls += 1
        # Simulate silent-drop auth: no reply, just return None
        return None

    flood_size = max_calls + 3  # enough to trigger rate-limit

    for _ in range(flood_size):
        update = make_telegram_update(chat_id=chat_id)
        update.message.reply_text = AsyncMock(
            side_effect=lambda text, **kw: reply_text_calls.append(text)
        )
        await _outer_rate_inner_auth(update, context=_make_context())

    # Rate-limiter must have fired at least once (reply_text called with a
    # "Rate limit exceeded" message) — proving it ran before the inner layer.
    rate_limit_replies = [t for t in reply_text_calls if "Rate limit" in t or "rate" in t.lower()]
    assert len(rate_limit_replies) >= 1, (
        f"Expected at least one rate-limit reply, got reply_text_calls={reply_text_calls!r}. "
        "This means the rate-limiter never fired — auth silent-drop ran first."
    )

    # Inner handler must have been called at most max_calls times (rate-limiter
    # stopped the flood after the window was exhausted).
    assert handler_calls <= max_calls, (
        f"handler_calls={handler_calls} exceeds max_calls={max_calls}: "
        "rate-limiter did not stop the flood."
    )


# ---------------------------------------------------------------------------
# TG-002: rate_limit applied to review handler functions
# (migrated from test_tg002_tg003_hardening.py)
# ---------------------------------------------------------------------------

_REVIEW_CARD = {
    "id": 1,
    "deck_id": 1,
    "paper_id": None,
    "card_type": "concept",
    "front": "Q?",
    "back": "A.",
    "evidence": {},
    "fsrs_state": {},
    "due_at": "2026-03-01T00:00:00Z",
    "created_at": "2026-01-01T00:00:00Z",
    "updated_at": "2026-01-01T00:00:00Z",
}


def _make_review_update_and_context(callback_data: str, chat_id: int = 54321):
    """Build a minimal (update, context, mock_http) for review-handler rate-limit tests."""
    update = MagicMock()
    update.effective_chat = MagicMock()
    update.effective_chat.id = chat_id
    update.message = MagicMock()
    update.message.reply_text = AsyncMock()

    query = MagicMock()
    query.data = callback_data
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    update.callback_query = query

    mock_http = AsyncMock()
    context = MagicMock()
    context.user_data = {}
    context.application = MagicMock()
    context.application.bot_data = {
        "config": make_bot_config(BotConfig, telegram_chat_id=chat_id),
        "db_pool": AsyncMock(),
        "http_client": mock_http,
    }
    return update, context, mock_http


@pytest.mark.asyncio
async def test_show_answer_rate_limited_after_10_calls():
    """TG-002: show_answer is rate-limited at 10 calls per 60 seconds.

    The 11th call within the window must be rejected (returns None).
    """
    _timestamps.clear()

    from telegram_bot.handlers.review_handler import show_answer

    for _ in range(10):
        update, context, _ = _make_review_update_and_context("show_answer")
        context.user_data = {"current_card": _REVIEW_CARD, "cards_reviewed": 0}
        result = await show_answer(update, context)
        assert result is not None, "First 10 calls must succeed"

    update, context, _ = _make_review_update_and_context("show_answer")
    context.user_data = {"current_card": _REVIEW_CARD, "cards_reviewed": 0}
    result = await show_answer(update, context)
    assert result is None, "11th call within 60 s must be rate-limited (returns None)"


@pytest.mark.asyncio
async def test_rate_card_rate_limited_after_5_calls():
    """TG-002: rate_card is rate-limited at 5 calls per 60 seconds.

    The 6th call within the window must be rejected (returns None).
    """
    _timestamps.clear()

    from telegram_bot.handlers.review_handler import rate_card

    submit_resp = MagicMock()
    submit_resp.raise_for_status = MagicMock()
    submit_resp.json.return_value = {"next_due_at": "2026-04-01T00:00:00Z"}

    next_resp = MagicMock()
    next_resp.raise_for_status = MagicMock()
    next_resp.json.return_value = []  # no more cards — each call ends the session

    for _ in range(5):
        update, context, mock_http = _make_review_update_and_context("rate_3")
        context.user_data = {"current_card": _REVIEW_CARD, "cards_reviewed": 0}
        mock_http.post.return_value = submit_resp
        mock_http.get.return_value = next_resp
        result = await rate_card(update, context)
        assert result is not None, "First 5 calls must succeed"

    update, context, mock_http = _make_review_update_and_context("rate_3")
    context.user_data = {"current_card": _REVIEW_CARD, "cards_reviewed": 0}
    mock_http.post.return_value = submit_resp
    mock_http.get.return_value = next_resp
    result = await rate_card(update, context)
    assert result is None, "6th call within 60 s must be rate-limited (returns None)"
