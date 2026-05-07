"""Unit tests for the rate_limit decorator.

Covers:
- TG-004: GC always prunes stale timestamps, not only when len > threshold.
  Previously an ``if len(stamps) > _GC_THRESHOLD`` guard meant that old
  timestamps for long-idle users were never pruned, skewing rate windows.
- Basic sliding-window enforcement.
- Basic cooldown enforcement.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram_bot.handlers.rate_limit import _timestamps, rate_limit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_update(chat_id: int = 12345) -> MagicMock:
    update = MagicMock()
    update.effective_chat = MagicMock()
    update.effective_chat.id = chat_id
    update.message = MagicMock()
    update.message.reply_text = AsyncMock()
    return update


def _make_context() -> MagicMock:
    return MagicMock()


# ---------------------------------------------------------------------------
# TG-004: unconditional GC
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limit_gc_always_prunes_stale_timestamps():
    """TG-004: stale timestamps are pruned even when count is below the old threshold.

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

    # Key format uses __module__.__qualname__ since W4-8; @wraps copies both to the wrapper.
    key = f"{chat_id}:{_noop_handler.__module__}.{_noop_handler.__qualname__}"
    _timestamps[key] = [far_past] * stale_count

    update = _make_update(chat_id=chat_id)
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
async def test_rate_limit_sliding_window_blocks_excess_calls():
    """Sliding-window: calls beyond max_calls within window are rejected."""
    _timestamps.clear()

    chat_id = 99002

    @rate_limit(max_calls=2, window_seconds=60)
    async def _guarded(update, context):  # type: ignore[no-untyped-def]
        return "ok"

    update = _make_update(chat_id=chat_id)
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

    update = _make_update(chat_id=chat_id)
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
    """W4-4: effective_chat is None — anonymous update bypasses the rate limiter.

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
    # Sprint 7 B11: covers the cooldown elif branch (the sliding-window
    # fallback was already covered above). When a callback handler is
    # decorated with cooldown_seconds and update.message is None, the
    # cooldown rejection must surface via callback_query.answer.
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
