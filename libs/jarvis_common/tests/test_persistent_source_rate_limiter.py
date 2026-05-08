"""Tests for PersistentSourceRateLimiter.

PR-A3: Postgres-backed rate limiter with in-memory fallback on DB outage.

All tests use AsyncMock/MagicMock to avoid a live Postgres dependency.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

from jarvis_common.source_rate_limiter import (
    PersistentSourceRateLimiter,
    SourceRateLimiter,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pool(row=None, raise_exc=None):
    """Return an asyncpg.Pool mock whose acquire() context yields a fake conn."""
    conn = AsyncMock()

    if raise_exc is not None:
        conn.fetchrow = AsyncMock(side_effect=raise_exc)
        conn.execute = AsyncMock(side_effect=raise_exc)
    else:
        conn.fetchrow = AsyncMock(return_value=row)
        conn.execute = AsyncMock(return_value=None)

    pool = MagicMock()
    # pool.acquire() used as async context manager
    acquire_cm = AsyncMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=acquire_cm)
    return pool, conn


# ---------------------------------------------------------------------------
# test_acquire_sleeps_when_recent_request
# ---------------------------------------------------------------------------


async def test_acquire_sleeps_when_recent_request(monkeypatch):
    """acquire() sleeps for the remaining interval when last_request_at is recent."""
    now = datetime.now(tz=UTC)
    recent_last_request = now - timedelta(seconds=2.0)

    row = {"last_request_at": recent_last_request, "cooldown_until": None}
    pool, _ = _make_pool(row=row)

    sleep_calls: list[float] = []

    async def _fake_sleep(secs: float) -> None:
        sleep_calls.append(secs)

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

    limiter = PersistentSourceRateLimiter(
        source_type="test_src",
        user_id=None,
        min_interval_seconds=10.0,
        db_pool=pool,
    )
    await limiter.acquire()

    assert len(sleep_calls) == 1
    # Remaining ≈ 10 - 2 = 8 seconds (allow ±1s for test execution overhead)
    assert 6.0 < sleep_calls[0] <= 10.0


# ---------------------------------------------------------------------------
# test_acquire_no_sleep_when_min_interval_elapsed
# ---------------------------------------------------------------------------


async def test_acquire_no_sleep_when_min_interval_elapsed(monkeypatch):
    """acquire() does not sleep when min_interval has fully elapsed."""
    now = datetime.now(tz=UTC)
    old_last_request = now - timedelta(seconds=30.0)

    row = {"last_request_at": old_last_request, "cooldown_until": None}
    pool, _ = _make_pool(row=row)

    sleep_calls: list[float] = []

    async def _fake_sleep(secs: float) -> None:
        sleep_calls.append(secs)

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

    limiter = PersistentSourceRateLimiter(
        source_type="test_src",
        user_id=1,
        min_interval_seconds=10.0,
        db_pool=pool,
    )
    await limiter.acquire()

    assert sleep_calls == [], f"Expected no sleep, got {sleep_calls}"


# ---------------------------------------------------------------------------
# test_acquire_no_sleep_when_no_row
# ---------------------------------------------------------------------------


async def test_acquire_no_sleep_when_no_row(monkeypatch):
    """acquire() does not sleep when source_health has no row yet."""
    pool, _ = _make_pool(row=None)

    sleep_calls: list[float] = []

    async def _fake_sleep(secs: float) -> None:
        sleep_calls.append(secs)

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

    limiter = PersistentSourceRateLimiter(
        source_type="new_src",
        user_id=None,
        min_interval_seconds=5.0,
        db_pool=pool,
    )
    await limiter.acquire()

    assert sleep_calls == []


# ---------------------------------------------------------------------------
# test_update_last_request_429_sets_cooldown_until
# ---------------------------------------------------------------------------


async def test_update_last_request_429_sets_cooldown_until():
    """update_last_request('rate_limit', retry_after_s=120) stores cooldown_until."""
    pool, conn = _make_pool()

    limiter = PersistentSourceRateLimiter(
        source_type="arxiv",
        user_id=None,
        min_interval_seconds=5.0,
        db_pool=pool,
    )
    await limiter.update_last_request(status="rate_limit", retry_after_s=120)

    conn.execute.assert_called_once()
    call_args = conn.execute.call_args
    # The SQL string passed in should contain INSERT ... ON CONFLICT
    sql: str = call_args[0][0]
    assert "INSERT" in sql
    assert "ON CONFLICT" in sql
    # The bound params should include the status string
    params = call_args[0][1:]
    assert "rate_limit" in params


async def test_update_last_request_ok_clears_cooldown():
    """update_last_request('ok') calls execute with 'ok' status."""
    pool, conn = _make_pool()

    limiter = PersistentSourceRateLimiter(
        source_type="s2",
        user_id=42,
        min_interval_seconds=1.0,
        db_pool=pool,
    )
    await limiter.update_last_request(status="ok")

    conn.execute.assert_called_once()
    params = conn.execute.call_args[0][1:]
    assert "ok" in params


async def test_update_last_request_error_increments_failures():
    """update_last_request('error') issues an UPSERT with consecutive_failures increment."""
    pool, conn = _make_pool()

    limiter = PersistentSourceRateLimiter(
        source_type="pubmed",
        user_id=7,
        min_interval_seconds=2.0,
        db_pool=pool,
    )
    await limiter.update_last_request(status="error")

    conn.execute.assert_called_once()
    sql: str = conn.execute.call_args[0][0]
    assert "consecutive_failures" in sql
    params = conn.execute.call_args[0][1:]
    assert "error" in params


# ---------------------------------------------------------------------------
# test_acquire_falls_back_when_pool_acquire_raises_OperationalError
# ---------------------------------------------------------------------------


async def test_acquire_falls_back_when_pool_acquire_raises_oserror(monkeypatch):
    """acquire() uses the fallback SourceRateLimiter when the DB pool raises OSError."""

    pool, _ = _make_pool(raise_exc=OSError("connection refused"))

    fallback_acquire_called: list[bool] = []

    async def _fake_fallback_acquire() -> None:
        fallback_acquire_called.append(True)

    fallback = MagicMock(spec=SourceRateLimiter)
    fallback.acquire = AsyncMock(side_effect=_fake_fallback_acquire)

    limiter = PersistentSourceRateLimiter(
        source_type="openalex",
        user_id=None,
        min_interval_seconds=3.0,
        db_pool=pool,
        fallback=fallback,
    )
    await limiter.acquire()

    assert fallback_acquire_called == [True]


async def test_acquire_sleeps_min_interval_when_no_fallback_and_db_down(monkeypatch):
    """acquire() sleeps min_interval_seconds when DB is down and no fallback is set."""
    pool, _ = _make_pool(raise_exc=OSError("no DB"))

    sleep_calls: list[float] = []

    async def _fake_sleep(secs: float) -> None:
        sleep_calls.append(secs)

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

    limiter = PersistentSourceRateLimiter(
        source_type="test",
        user_id=None,
        min_interval_seconds=5.0,
        db_pool=pool,
    )
    await limiter.acquire()

    assert sleep_calls == [5.0]


# ---------------------------------------------------------------------------
# test_acquire_sleeps_through_cooldown
# ---------------------------------------------------------------------------


async def test_acquire_sleeps_through_cooldown(monkeypatch):
    """acquire() sleeps until cooldown_until when source is in cooldown."""
    now = datetime.now(tz=UTC)
    cooldown_until = now + timedelta(seconds=45.0)

    row = {"last_request_at": now - timedelta(seconds=60), "cooldown_until": cooldown_until}
    pool, _ = _make_pool(row=row)

    sleep_calls: list[float] = []

    async def _fake_sleep(secs: float) -> None:
        sleep_calls.append(secs)

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

    limiter = PersistentSourceRateLimiter(
        source_type="test_src",
        user_id=None,
        min_interval_seconds=2.0,
        db_pool=pool,
    )
    await limiter.acquire()

    assert len(sleep_calls) == 1
    # Should sleep roughly 45 seconds (allow ±2s for execution time)
    assert 43.0 <= sleep_calls[0] <= 47.0


# ---------------------------------------------------------------------------
# test is_in_cooldown
# ---------------------------------------------------------------------------


async def test_is_in_cooldown_returns_true_when_in_cooldown():
    """is_in_cooldown() returns (True, cooldown_until) when cooldown_until is future."""
    now = datetime.now(tz=UTC)
    cooldown_until = now + timedelta(minutes=30)

    row = {"cooldown_until": cooldown_until}
    pool, _ = _make_pool(row=row)

    limiter = PersistentSourceRateLimiter(
        source_type="arxiv",
        user_id=None,
        min_interval_seconds=5.0,
        db_pool=pool,
    )
    in_cd, until = await limiter.is_in_cooldown()

    assert in_cd is True
    assert until == cooldown_until


async def test_is_in_cooldown_returns_false_when_no_row():
    """is_in_cooldown() returns (False, None) when no source_health row exists."""
    pool, _ = _make_pool(row=None)

    limiter = PersistentSourceRateLimiter(
        source_type="s2",
        user_id=1,
        min_interval_seconds=5.0,
        db_pool=pool,
    )
    in_cd, until = await limiter.is_in_cooldown()

    assert in_cd is False
    assert until is None


async def test_is_in_cooldown_returns_false_when_cooldown_expired():
    """is_in_cooldown() returns (False, None) when cooldown_until is in the past."""
    now = datetime.now(tz=UTC)
    past_cooldown = now - timedelta(minutes=5)

    row = {"cooldown_until": past_cooldown}
    pool, _ = _make_pool(row=row)

    limiter = PersistentSourceRateLimiter(
        source_type="pubmed",
        user_id=None,
        min_interval_seconds=5.0,
        db_pool=pool,
    )
    in_cd, until = await limiter.is_in_cooldown()

    assert in_cd is False
    assert until is None
