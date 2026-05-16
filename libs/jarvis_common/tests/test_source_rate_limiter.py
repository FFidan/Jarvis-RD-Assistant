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


# ---------------------------------------------------------------------------
# B2: PersistentSourceRateLimiter.reset() and .health_snapshot()
# ---------------------------------------------------------------------------


def _make_pool(fetchrow_return=None, raise_exc=None):
    """asyncpg.Pool mock whose acquire() context yields a fresh conn.

    Mirrors the helper in test_persistent_source_rate_limiter.py.
    """
    from unittest.mock import AsyncMock, MagicMock

    conn = AsyncMock()
    if raise_exc is not None:
        conn.fetchrow = AsyncMock(side_effect=raise_exc)
        conn.execute = AsyncMock(side_effect=raise_exc)
    else:
        conn.fetchrow = AsyncMock(return_value=fetchrow_return)
        conn.execute = AsyncMock(return_value=None)

    pool = MagicMock()
    acquire_cm = AsyncMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=acquire_cm)
    return pool, conn


@pytest.mark.asyncio
async def test_reset_issues_upsert_clearing_cooldown_and_failures():
    """reset() UPSERTs last_status='ok', cooldown_until=NULL, failures=0."""
    from jarvis_common.source_rate_limiter import PersistentSourceRateLimiter

    pool, conn = _make_pool()
    limiter = PersistentSourceRateLimiter(
        source_type="arxiv",
        user_id=None,
        min_interval_seconds=3.0,
        db_pool=pool,
    )

    await limiter.reset()

    conn.execute.assert_called_once()
    sql: str = conn.execute.call_args[0][0]
    assert "INSERT" in sql and "ON CONFLICT" in sql
    assert "cooldown_until = NULL" in sql
    assert "consecutive_failures = 0" in sql
    assert "'ok'" in sql
    # Bound params: user_id, source_type, now
    params = conn.execute.call_args[0][1:]
    assert "arxiv" in params


@pytest.mark.asyncio
async def test_reset_swallows_db_error():
    """reset() never raises even when the DB pool errors."""
    from jarvis_common.source_rate_limiter import PersistentSourceRateLimiter

    pool, _ = _make_pool(raise_exc=OSError("db down"))
    limiter = PersistentSourceRateLimiter(
        source_type="arxiv",
        user_id=7,
        min_interval_seconds=3.0,
        db_pool=pool,
    )

    # Must not raise.
    await limiter.reset()


@pytest.mark.asyncio
async def test_health_snapshot_in_cooldown():
    """Future cooldown_until → in_cooldown True, stale False."""
    from datetime import UTC, datetime, timedelta

    from jarvis_common.source_rate_limiter import PersistentSourceRateLimiter

    until = datetime.now(tz=UTC) + timedelta(hours=1)
    last_req = datetime.now(tz=UTC) - timedelta(minutes=5)
    pool, _ = _make_pool(
        fetchrow_return={
            "cooldown_until": until,
            "last_status": "rate_limit",
            "last_request_at": last_req,
        }
    )
    limiter = PersistentSourceRateLimiter(
        source_type="arxiv",
        user_id=None,
        min_interval_seconds=3.0,
        db_pool=pool,
    )

    snap = await limiter.health_snapshot()

    assert snap["in_cooldown"] is True
    assert snap["stale"] is False
    assert snap["cooldown_until"] == until.isoformat()
    assert snap["last_status"] == "rate_limit"
    assert snap["last_request_at"] == last_req.isoformat()


@pytest.mark.asyncio
async def test_health_snapshot_stale_when_rate_limit_and_cooldown_expired():
    """rate_limit + past cooldown_until → stale True, in_cooldown False.

    This is exactly the stuck arXiv state from the bug report: a genuine 429
    set rate_limit + cooldown, the cooldown lapsed, nothing reset it.
    """
    from datetime import UTC, datetime, timedelta

    from jarvis_common.source_rate_limiter import PersistentSourceRateLimiter

    past = datetime.now(tz=UTC) - timedelta(days=7)
    pool, _ = _make_pool(
        fetchrow_return={
            "cooldown_until": past,
            "last_status": "rate_limit",
            "last_request_at": None,
        }
    )
    limiter = PersistentSourceRateLimiter(
        source_type="arxiv",
        user_id=None,
        min_interval_seconds=3.0,
        db_pool=pool,
    )

    snap = await limiter.health_snapshot()

    assert snap["in_cooldown"] is False
    assert snap["stale"] is True
    assert snap["last_status"] == "rate_limit"
    assert snap["last_request_at"] is None


@pytest.mark.asyncio
async def test_health_snapshot_stale_when_rate_limit_and_no_cooldown():
    """rate_limit + NULL cooldown_until → stale True (null-or-past rule)."""
    from jarvis_common.source_rate_limiter import PersistentSourceRateLimiter

    pool, _ = _make_pool(
        fetchrow_return={
            "cooldown_until": None,
            "last_status": "rate_limit",
            "last_request_at": None,
        }
    )
    limiter = PersistentSourceRateLimiter(
        source_type="arxiv",
        user_id=None,
        min_interval_seconds=3.0,
        db_pool=pool,
    )

    snap = await limiter.health_snapshot()

    assert snap["stale"] is True
    assert snap["in_cooldown"] is False
    assert snap["cooldown_until"] is None


@pytest.mark.asyncio
async def test_health_snapshot_fresh_ok_not_stale():
    """last_status='ok' is never stale and never in cooldown."""
    from datetime import UTC, datetime

    from jarvis_common.source_rate_limiter import PersistentSourceRateLimiter

    now = datetime.now(tz=UTC)
    pool, _ = _make_pool(
        fetchrow_return={
            "cooldown_until": None,
            "last_status": "ok",
            "last_request_at": now,
        }
    )
    limiter = PersistentSourceRateLimiter(
        source_type="arxiv",
        user_id=None,
        min_interval_seconds=3.0,
        db_pool=pool,
    )

    snap = await limiter.health_snapshot()

    assert snap["in_cooldown"] is False
    assert snap["stale"] is False
    assert snap["last_status"] == "ok"


@pytest.mark.asyncio
async def test_health_snapshot_no_row_returns_safe_default():
    """No source_health row → safe all-default snapshot."""
    from jarvis_common.source_rate_limiter import PersistentSourceRateLimiter

    pool, _ = _make_pool(fetchrow_return=None)
    limiter = PersistentSourceRateLimiter(
        source_type="arxiv",
        user_id=None,
        min_interval_seconds=3.0,
        db_pool=pool,
    )

    snap = await limiter.health_snapshot()

    assert snap == {
        "in_cooldown": False,
        "cooldown_until": None,
        "last_status": None,
        "last_request_at": None,
        "stale": False,
    }


@pytest.mark.asyncio
async def test_health_snapshot_db_error_returns_safe_default():
    """A DB error yields the safe default snapshot, never raises."""
    from jarvis_common.source_rate_limiter import PersistentSourceRateLimiter

    pool, _ = _make_pool(raise_exc=OSError("db down"))
    limiter = PersistentSourceRateLimiter(
        source_type="arxiv",
        user_id=None,
        min_interval_seconds=3.0,
        db_pool=pool,
    )

    snap = await limiter.health_snapshot()

    assert snap["in_cooldown"] is False
    assert snap["stale"] is False
    assert snap["last_status"] is None
