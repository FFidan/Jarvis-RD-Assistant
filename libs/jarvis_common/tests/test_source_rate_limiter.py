"""Tests for SourceRateLimiter (token-bucket rate limiter for source plugins)."""

from __future__ import annotations

import pytest


def test_rate_limiter_sync_instantiation():
    """SourceRateLimiter can be instantiated without a running event loop."""
    from jarvis_common.source_rate_limiter import SourceRateLimiter

    limiter = SourceRateLimiter(requests_per_minute=10)
    assert limiter is not None


def test_rate_limiter_requests_per_minute_conversion():
    """requests_per_minute kwarg is correctly converted to rate_per_second."""
    from jarvis_common.source_rate_limiter import SourceRateLimiter

    limiter = SourceRateLimiter(requests_per_minute=60)
    assert limiter.rate == pytest.approx(1.0)


def test_rate_limiter_rate_per_second_direct():
    """rate_per_second positional arg is stored as-is."""
    from jarvis_common.source_rate_limiter import SourceRateLimiter

    limiter = SourceRateLimiter(rate_per_second=2.5)
    assert limiter.rate == pytest.approx(2.5)


def test_rate_limiter_initial_tokens_equal_burst():
    """Token bucket starts full (tokens == burst capacity)."""
    from jarvis_common.source_rate_limiter import SourceRateLimiter

    limiter = SourceRateLimiter(rate_per_second=1.0, burst=5)
    assert limiter.tokens == pytest.approx(5.0)
    assert limiter.capacity == pytest.approx(5.0)


@pytest.mark.asyncio
async def test_rate_limiter_acquire_decrements_tokens():
    """acquire() decrements the token counter by 1 when tokens are available."""
    from jarvis_common.source_rate_limiter import SourceRateLimiter

    limiter = SourceRateLimiter(rate_per_second=10.0, burst=3)
    await limiter.acquire()
    assert limiter.tokens == pytest.approx(2.0, abs=0.1)


def test_rate_limiter_zero_rate_raises():
    """rate_per_second=0 raises ValueError at construction time."""
    from jarvis_common.source_rate_limiter import SourceRateLimiter

    with pytest.raises(ValueError, match="rate_per_second must be > 0"):
        SourceRateLimiter(rate_per_second=0.0)


def test_rate_limiter_negative_rate_raises():
    """Negative rate_per_second raises ValueError."""
    from jarvis_common.source_rate_limiter import SourceRateLimiter

    with pytest.raises(ValueError):
        SourceRateLimiter(rate_per_second=-5.0)


@pytest.mark.asyncio
async def test_rate_limiter_acquire_multiple_within_burst(monkeypatch):
    """Multiple acquires within burst capacity do not sleep (no asyncio.sleep call)."""
    import asyncio

    from jarvis_common.source_rate_limiter import SourceRateLimiter

    sleep_calls: list[float] = []

    async def _fake_sleep(secs: float) -> None:
        sleep_calls.append(secs)

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

    limiter = SourceRateLimiter(rate_per_second=100.0, burst=5)
    for _ in range(5):
        await limiter.acquire()

    assert sleep_calls == [], "No sleep expected when burst capacity covers all acquires"
    assert limiter.tokens == pytest.approx(0.0, abs=0.1)


@pytest.mark.asyncio
async def test_rate_limiter_acquire_empty_bucket_sleeps(monkeypatch):
    """acquire() on an empty bucket calls asyncio.sleep with a positive wait time."""
    import asyncio

    from jarvis_common.source_rate_limiter import SourceRateLimiter

    sleep_calls: list[float] = []

    async def _fake_sleep(secs: float) -> None:
        sleep_calls.append(secs)

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

    # burst=1 — first acquire drains the bucket; second must sleep
    limiter = SourceRateLimiter(rate_per_second=1.0, burst=1)
    await limiter.acquire()  # drains tokens to 0
    await limiter.acquire()  # bucket empty → sleep

    assert len(sleep_calls) == 1
    assert sleep_calls[0] > 0.0


@pytest.mark.asyncio
async def test_rate_limiter_bucket_reset_after_sleep(monkeypatch):
    """JC-003: after sleeping on empty bucket, tokens==0 and last_refill is fresh.

    Without the fix, last_refill was stale (set before the sleep), so the next
    refill would count the sleep duration as elapsed time and over-fill the bucket.
    """
    import asyncio
    import time

    from jarvis_common.source_rate_limiter import SourceRateLimiter

    async def _fake_sleep(secs: float) -> None:
        # Don't actually sleep; just simulate the sleep completing.
        pass

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

    before = time.monotonic()

    # burst=1 — first acquire drains; second hits the empty-bucket path.
    limiter = SourceRateLimiter(rate_per_second=1.0, burst=1)
    await limiter.acquire()  # drains tokens to 0
    await limiter.acquire()  # sleeps (mocked), then resets

    after = time.monotonic()

    # After the sleep, tokens must be 0 (not refilled from stale elapsed time).
    assert limiter.tokens == pytest.approx(0.0, abs=1e-9)
    # last_refill must be set to the time after the sleep, not before it.
    assert limiter.last_refill >= before
    assert limiter.last_refill <= after + 0.1  # within a reasonable window
