"""Tests for SourceRateLimiter (token-bucket rate limiter for source plugins)."""

from __future__ import annotations

import pytest


def test_rate_limiter_sync_instantiation():
    """SourceRateLimiter can be instantiated without a running event loop."""
    from jarvis_common.rate_limiter import SourceRateLimiter

    limiter = SourceRateLimiter(requests_per_minute=10)
    assert limiter is not None


def test_rate_limiter_requests_per_minute_conversion():
    """requests_per_minute kwarg is correctly converted to rate_per_second."""
    from jarvis_common.rate_limiter import SourceRateLimiter

    limiter = SourceRateLimiter(requests_per_minute=60)
    assert limiter.rate == pytest.approx(1.0)


def test_rate_limiter_rate_per_second_direct():
    """rate_per_second positional arg is stored as-is."""
    from jarvis_common.rate_limiter import SourceRateLimiter

    limiter = SourceRateLimiter(rate_per_second=2.5)
    assert limiter.rate == pytest.approx(2.5)


def test_rate_limiter_initial_tokens_equal_burst():
    """Token bucket starts full (tokens == burst capacity)."""
    from jarvis_common.rate_limiter import SourceRateLimiter

    limiter = SourceRateLimiter(rate_per_second=1.0, burst=5)
    assert limiter.tokens == pytest.approx(5.0)
    assert limiter.capacity == pytest.approx(5.0)


@pytest.mark.asyncio
async def test_rate_limiter_acquire_decrements_tokens():
    """acquire() decrements the token counter by 1 when tokens are available."""
    from jarvis_common.rate_limiter import SourceRateLimiter

    limiter = SourceRateLimiter(rate_per_second=10.0, burst=3)
    await limiter.acquire()
    assert limiter.tokens == pytest.approx(2.0, abs=0.1)
