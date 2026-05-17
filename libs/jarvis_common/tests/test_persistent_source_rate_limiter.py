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


def _make_pool(fetchrow_side_effects=None, raise_exc=None):
    """Return an asyncpg.Pool mock whose acquire() context yields a fresh conn.

    Parameters
    ----------
    fetchrow_side_effects:
        A list of return values (or exceptions) for successive ``conn.fetchrow``
        calls.  If omitted, all calls return ``None``.
    raise_exc:
        If set, every ``fetchrow`` / ``execute`` call raises this exception.
    """
    conn = AsyncMock()

    if raise_exc is not None:
        conn.fetchrow = AsyncMock(side_effect=raise_exc)
        conn.execute = AsyncMock(side_effect=raise_exc)
    else:
        if fetchrow_side_effects is not None:
            conn.fetchrow = AsyncMock(side_effect=fetchrow_side_effects)
        else:
            conn.fetchrow = AsyncMock(return_value=None)
        conn.execute = AsyncMock(return_value=None)

    # conn.transaction() is called as a plain method (not awaited) and must
    # return an async context manager.  Wire it up explicitly so it does not
    # inadvertently return a coroutine (which AsyncMock would do by default).
    txn_cm = AsyncMock()
    txn_cm.__aenter__ = AsyncMock(return_value=None)
    txn_cm.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn_cm)

    pool = MagicMock()
    # pool.acquire() used as async context manager; always returns the same conn
    acquire_cm = AsyncMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=acquire_cm)
    return pool, conn


# ---------------------------------------------------------------------------
# acquire() — slot claimed immediately (interval elapsed)
# ---------------------------------------------------------------------------


async def test_acquire_no_sleep_when_min_interval_elapsed(monkeypatch):
    """acquire() proceeds without sleeping when min_interval has fully elapsed.

    fetchrow sequence:
      1. cooldown check  → no row (no cooldown)
      2. atomic claim    → row returned (claim won)
    """
    now = datetime.now(tz=UTC)
    claim_row = {"last_request_at": now}

    pool, _ = _make_pool(fetchrow_side_effects=[None, claim_row])

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
# acquire() — slot taken, then sleeps, then retries successfully
# ---------------------------------------------------------------------------


async def test_acquire_sleeps_when_recent_request(monkeypatch):
    """acquire() sleeps for the remaining interval when slot is taken.

    fetchrow sequence:
      1. cooldown check    → no row
      2. atomic claim      → None (slot taken by another worker)
      3. wait-row read     → last_request_at = 2s ago
      4. retry claim       → row returned (claim won after sleep)
    """
    now = datetime.now(tz=UTC)
    recent_last_request = now - timedelta(seconds=2.0)
    wait_row = {"last_request_at": recent_last_request}
    claim_row = {"last_request_at": now}

    pool, _ = _make_pool(fetchrow_side_effects=[None, None, wait_row, claim_row])

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
# acquire() — no row yet (first-ever request for source)
# ---------------------------------------------------------------------------


async def test_acquire_no_sleep_when_no_row(monkeypatch):
    """acquire() proceeds without sleeping when source_health has no row yet.

    The INSERT path always succeeds for a new source (no conflict), so the
    atomic claim returns a row immediately.

    fetchrow sequence:
      1. cooldown check → None (no row)
      2. atomic claim   → row returned (INSERT path, no conflict)
    """
    now = datetime.now(tz=UTC)
    claim_row = {"last_request_at": now}

    pool, _ = _make_pool(fetchrow_side_effects=[None, claim_row])

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
# acquire() — cooldown path
# ---------------------------------------------------------------------------


async def test_acquire_sleeps_through_cooldown(monkeypatch):
    """acquire() sleeps until cooldown_until when source is in cooldown."""
    now = datetime.now(tz=UTC)
    cooldown_until = now + timedelta(seconds=45.0)

    cooldown_row = {"cooldown_until": cooldown_until}
    # Only the cooldown check fetchrow is called; method returns early.
    pool, _ = _make_pool(fetchrow_side_effects=[cooldown_row])

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
# acquire() — DB failure paths
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
# update_last_request tests (unchanged behaviour)
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
# is_in_cooldown tests (unchanged behaviour)
# ---------------------------------------------------------------------------


async def test_is_in_cooldown_returns_true_when_in_cooldown():
    """is_in_cooldown() returns (True, cooldown_until) when cooldown_until is future."""
    now = datetime.now(tz=UTC)
    cooldown_until = now + timedelta(minutes=30)

    row = {"cooldown_until": cooldown_until}
    pool, _ = _make_pool(fetchrow_side_effects=[row])

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
    pool, _ = _make_pool(fetchrow_side_effects=[None])

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
    pool, _ = _make_pool(fetchrow_side_effects=[row])

    limiter = PersistentSourceRateLimiter(
        source_type="pubmed",
        user_id=None,
        min_interval_seconds=5.0,
        db_pool=pool,
    )
    in_cd, until = await limiter.is_in_cooldown()

    assert in_cd is False
    assert until is None


# ---------------------------------------------------------------------------
# H-1: Concurrency test — exactly ONE worker proceeds per interval
# ---------------------------------------------------------------------------


async def test_concurrent_acquire_only_one_proceeds_without_sleep(monkeypatch):
    """Exactly one of two concurrent acquire() calls proceeds without sleeping.

    Simulates the atomic slot claim: the first worker wins the claim
    (fetchrow returns a row); the second worker gets None (slot taken),
    reads the wait time, sleeps, then wins on retry.

    We use separate pool mocks per limiter instance so each coroutine's
    fetchrow calls are independent — matching production behaviour where
    each worker holds its own DB connection.

    Assertion: exactly one sleep occurs (the losing worker's throttle wait),
    not zero (both would fire) and not two (would mean double-penalising).
    """
    now = datetime.now(tz=UTC)
    recent = now - timedelta(seconds=1.0)  # 1s ago → 9s wait for 10s interval

    # Worker A: wins the claim immediately.
    #   call 1 → cooldown check → None
    #   call 2 → atomic claim   → row (won)
    claim_row = {"last_request_at": now}
    pool_a, _ = _make_pool(fetchrow_side_effects=[None, claim_row])

    # Worker B: loses the claim, sleeps, then retries and wins.
    #   call 1 → cooldown check → None
    #   call 2 → atomic claim   → None (slot taken)
    #   call 3 → wait-row read  → last_request_at = 1s ago → wait ~9s
    #   call 4 → retry claim    → row (won)
    wait_row = {"last_request_at": recent}
    pool_b, _ = _make_pool(fetchrow_side_effects=[None, None, wait_row, claim_row])

    sleep_calls: list[float] = []

    async def _fake_sleep(secs: float) -> None:
        sleep_calls.append(secs)

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

    limiter_a = PersistentSourceRateLimiter(
        source_type="arxiv",
        user_id=None,
        min_interval_seconds=10.0,
        db_pool=pool_a,
    )
    limiter_b = PersistentSourceRateLimiter(
        source_type="arxiv",
        user_id=None,
        min_interval_seconds=10.0,
        db_pool=pool_b,
    )

    await asyncio.gather(limiter_a.acquire(), limiter_b.acquire())

    # Worker A: 0 sleeps.  Worker B: 1 sleep (throttle wait ≈ 9s).
    assert len(sleep_calls) == 1, (
        f"Expected exactly 1 sleep (losing worker throttles); got {sleep_calls}"
    )
    assert 7.0 < sleep_calls[0] <= 10.0, (
        f"Sleep duration {sleep_calls[0]:.2f}s out of expected range (7–10s)"
    )


# ---------------------------------------------------------------------------
# M-2: Atomic cooldown-check + claim (PI-7)
# ---------------------------------------------------------------------------


async def test_cooldown_observed_inside_same_transaction(monkeypatch):
    """M-2a: cooldown set by a concurrent worker is observed atomically.

    The fix wraps the cooldown check inside a SELECT … FOR UPDATE transaction
    so a cooldown written between the check and the claim is never missed.
    When the locked-row read returns a future cooldown_until, the limiter
    sleeps and does NOT attempt the slot claim (only 1 fetchrow total).

    fetchrow sequence (single connection, single transaction):
      1. SELECT … FOR UPDATE → locked_row with future cooldown_until
      (method returns after cooldown sleep — no slot-claim fetchrow)
    """
    now = datetime.now(tz=UTC)
    cooldown_until = now + timedelta(seconds=30.0)

    # Simulate a cooldown that a concurrent update_last_request("rate_limit")
    # wrote — the FOR UPDATE lock ensures our check sees it.
    locked_row = {"cooldown_until": cooldown_until, "last_request_at": now}
    pool, conn = _make_pool(fetchrow_side_effects=[locked_row])

    sleep_calls: list[float] = []

    async def _fake_sleep(secs: float) -> None:
        sleep_calls.append(secs)

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

    limiter = PersistentSourceRateLimiter(
        source_type="arxiv",
        user_id=None,
        min_interval_seconds=5.0,
        db_pool=pool,
    )
    await limiter.acquire()

    # Must have slept for the cooldown duration (≈ 30s).
    assert len(sleep_calls) == 1, f"Expected 1 cooldown sleep; got {sleep_calls}"
    assert 28.0 <= sleep_calls[0] <= 32.0, (
        f"Cooldown sleep {sleep_calls[0]:.2f}s out of expected range"
    )
    # Slot-claim fetchrow must NOT have been called — only 1 fetchrow total.
    assert conn.fetchrow.call_count == 1, (
        f"Expected exactly 1 fetchrow (cooldown check only); got {conn.fetchrow.call_count}"
    )


async def test_second_claim_failure_logs_warning_and_raises(monkeypatch, caplog):
    """M-2b: a 2nd failed slot claim logs a WARNING and raises (not silent).

    Previously the 2nd attempt failing would fall through silently, allowing
    a rate-limit bypass.  After the fix, a RuntimeError is raised so that
    acquire() triggers the fallback path — and a WARNING is emitted.

    fetchrow sequence:
      1. SELECT … FOR UPDATE → None (no existing row / no cooldown)
      2. atomic claim (attempt 0) → None (slot taken)
      3. wait-row read           → last_request_at = 1s ago → sleep ~9s
      4. atomic claim (attempt 1) → None (slot STILL taken → 2nd failure)
    """
    import logging

    now = datetime.now(tz=UTC)
    recent = now - timedelta(seconds=1.0)
    wait_row = {"last_request_at": recent}

    # All claim attempts fail (None); wait-row is readable on the 3rd call.
    pool, _ = _make_pool(fetchrow_side_effects=[None, None, wait_row, None])

    sleep_calls: list[float] = []

    async def _fake_sleep(secs: float) -> None:
        sleep_calls.append(secs)

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

    limiter = PersistentSourceRateLimiter(
        source_type="s2",
        user_id=1,
        min_interval_seconds=10.0,
        db_pool=pool,
    )

    # acquire() wraps _acquire_with_retry; the RuntimeError from the 2nd
    # claim failure triggers the fallback (bare asyncio.sleep(min_interval)).
    with caplog.at_level(logging.WARNING, logger="jarvis_common.source_rate_limiter"):
        await limiter.acquire()

    # Total sleeps: 1 throttle-wait (≈9s from attempt-0 retry) + 1 fallback (10s).
    assert len(sleep_calls) == 2, f"Expected 2 sleeps (throttle + fallback); got {sleep_calls}"
    assert 7.0 < sleep_calls[0] <= 10.0, (
        f"Throttle-wait sleep {sleep_calls[0]:.2f}s out of range (7–10s)"
    )
    assert sleep_calls[1] == 10.0, (
        f"Fallback sleep should be min_interval=10s; got {sleep_calls[1]}"
    )

    # A WARNING about the 2nd claim failure must appear in the log.
    warning_messages = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("slot still taken after retry" in m for m in warning_messages), (
        f"Expected 'slot still taken after retry' warning; got: {warning_messages}"
    )
