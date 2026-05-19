"""Tests for PersistentSourceRateLimiter.

PR-A3: Postgres-backed rate limiter with in-memory fallback on DB outage.

Most tests use AsyncMock/MagicMock to avoid a live Postgres dependency.
The live-PG race test (test_m2_race_live_pg_*) requires JARVIS_RUN_LIVE_PG=1
and a running Docker daemon; it is skipped by default.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
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

    fetchrow sequence (single transaction):
      1. SELECT … FOR UPDATE → None (no existing row)
      2. INSERT … RETURNING  → claim_row (INSERT path, no conflict)
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

    fetchrow sequence (new unified-transaction design):

    Attempt 0 (single txn):
      1. SELECT … FOR UPDATE → {last_request_at=2s_ago, cooldown_until=None}
         (row exists, no cooldown; wait computed from last_request_at in locked_row)
      2. INSERT … RETURNING  → None (slot taken by another worker)

    Attempt 1 (single txn after sleep):
      3. SELECT … FOR UPDATE → None (no row to lock, or row now stale)
      4. INSERT … RETURNING  → claim_row (INSERT path, claim won)
    """
    now = datetime.now(tz=UTC)
    recent_last_request = now - timedelta(seconds=2.0)
    locked_row = {"last_request_at": recent_last_request, "cooldown_until": None}
    claim_row = {"last_request_at": now}

    pool, _ = _make_pool(fetchrow_side_effects=[locked_row, None, None, claim_row])

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

    fetchrow sequence (single transaction):
      1. SELECT … FOR UPDATE → None (no row)
      2. INSERT … RETURNING  → claim_row (INSERT path, no conflict)
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
    """acquire() sleeps until cooldown_until when source is in cooldown.

    fetchrow sequence (single transaction):
      1. SELECT … FOR UPDATE → {cooldown_until=45s_future, last_request_at=...}
         (cooldown active — no INSERT issued)
    """
    now = datetime.now(tz=UTC)
    cooldown_until = now + timedelta(seconds=45.0)

    cooldown_row = {"cooldown_until": cooldown_until, "last_request_at": now}
    # Only the FOR UPDATE fetch is called; the INSERT is skipped.
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
    reads the wait time from the locked_row, sleeps, then wins on retry.

    We use separate pool mocks per limiter instance so each coroutine's
    fetchrow calls are independent — matching production behaviour where
    each worker holds its own DB connection.

    Assertion: exactly one sleep occurs (the losing worker's throttle wait),
    not zero (both would fire) and not two (would mean double-penalising).

    fetchrow sequence (new unified-transaction design):

    Worker A (wins immediately, single txn):
      1. SELECT … FOR UPDATE → None  (no existing row)
      2. INSERT … RETURNING  → claim_row

    Worker B (loses attempt 0, retries attempt 1):
      Attempt 0 txn:
        1. SELECT … FOR UPDATE → {last_request_at=1s_ago, cooldown_until=None}
        2. INSERT … RETURNING  → None (slot taken)
      Sleep ~9s (computed from locked_row["last_request_at"])
      Attempt 1 txn:
        3. SELECT … FOR UPDATE → None
        4. INSERT … RETURNING  → claim_row
    """
    now = datetime.now(tz=UTC)
    recent = now - timedelta(seconds=1.0)  # 1s ago → 9s wait for 10s interval

    # Worker A: wins the claim immediately (no existing row).
    claim_row = {"last_request_at": now}
    pool_a, _ = _make_pool(fetchrow_side_effects=[None, claim_row])

    # Worker B: loses attempt 0, sleeps using last_request_at from locked_row,
    # then wins on attempt 1.
    locked_row_b = {"last_request_at": recent, "cooldown_until": None}
    pool_b, _ = _make_pool(fetchrow_side_effects=[locked_row_b, None, None, claim_row])

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

    The FOR UPDATE and the slot claim now share ONE transaction on ONE
    connection.  When the locked-row read returns a future cooldown_until,
    the limiter sleeps and does NOT attempt the slot claim (only 1 fetchrow
    per attempt).

    fetchrow sequence (single connection, single transaction):
      1. SELECT … FOR UPDATE → locked_row with future cooldown_until
      (INSERT claim is skipped — no 2nd fetchrow)
    """
    now = datetime.now(tz=UTC)
    cooldown_until = now + timedelta(seconds=30.0)

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
    # INSERT claim fetchrow must NOT have been called — only 1 fetchrow total.
    assert conn.fetchrow.call_count == 1, (
        f"Expected exactly 1 fetchrow (FOR UPDATE check only, no claim); "
        f"got {conn.fetchrow.call_count}"
    )


async def test_m2_race_cooldown_observed_not_bypassed(monkeypatch):
    """M-2b (race guard): a cooldown written before FOR UPDATE is never bypassed.

    This test demonstrates why the fix is correct: the FOR UPDATE lock and the
    slot claim share ONE transaction.  If the FOR UPDATE step sees
    ``cooldown_until`` set (as it would when ``update_last_request("rate_limit")``
    committed before FOR UPDATE acquired the row lock), the INSERT claim is
    never issued — proving there is no window between check and claim.

    Concretely: two fetchrow side-effects are registered, but only ONE is
    consumed (the FOR UPDATE read).  The INSERT claim (call 2) is skipped
    entirely.  If check and claim were in separate transactions, both calls
    would be consumed regardless of the cooldown value in call 1.

    fetchrow sequence:
      1. SELECT … FOR UPDATE → {cooldown_until=60s_future, last_request_at=...}
         (simulates update_last_request("rate_limit") having committed first)
      2. INSERT … RETURNING  → (would be a bypass if reached — test fails if
         this call is consumed)
    """
    now = datetime.now(tz=UTC)
    cooldown_until = now + timedelta(seconds=60.0)

    # The second entry would represent a successful claim — if it is ever
    # reached, the cooldown was bypassed (the test would count 0 sleeps or
    # wrong fetchrow_call_count).
    bypass_sentinel = {"last_request_at": now}
    locked_row = {"cooldown_until": cooldown_until, "last_request_at": now}
    pool, conn = _make_pool(fetchrow_side_effects=[locked_row, bypass_sentinel])

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

    # If the race were open, the INSERT claim would be attempted (call_count==2)
    # and the caller would return without sleeping.
    assert conn.fetchrow.call_count == 1, (
        f"INSERT claim was issued despite active cooldown — race window is open! "
        f"fetchrow was called {conn.fetchrow.call_count} times"
    )
    # Must have slept through the cooldown (≈ 60s).
    assert len(sleep_calls) == 1, (
        f"Expected 1 cooldown sleep; got {sleep_calls} — cooldown may have been bypassed"
    )
    assert 58.0 <= sleep_calls[0] <= 62.0, (
        f"Cooldown sleep {sleep_calls[0]:.2f}s out of expected range (58–62s)"
    )


async def test_second_claim_failure_logs_warning_and_raises(monkeypatch, caplog):
    """M-2c: a 2nd failed slot claim logs a WARNING and raises (not silent).

    Previously the 2nd attempt failing would fall through silently, allowing
    a rate-limit bypass.  After the fix, a RuntimeError is raised so that
    acquire() triggers the fallback path — and a WARNING is emitted.

    fetchrow sequence (new unified-transaction design):

    Attempt 0 (single txn):
      1. SELECT … FOR UPDATE → {last_request_at=1s_ago, cooldown_until=None}
      2. INSERT … RETURNING  → None (slot taken)
         wait computed from locked_row["last_request_at"] → sleep ~9s

    Attempt 1 (single txn after sleep):
      3. SELECT … FOR UPDATE → None (no lock row)
      4. INSERT … RETURNING  → None (slot STILL taken → 2nd failure → RuntimeError)
    """
    import logging

    now = datetime.now(tz=UTC)
    recent = now - timedelta(seconds=1.0)
    locked_row = {"last_request_at": recent, "cooldown_until": None}

    # attempt 0: [locked_row, None]; attempt 1: [None, None]
    pool, _ = _make_pool(fetchrow_side_effects=[locked_row, None, None, None])

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


# ---------------------------------------------------------------------------
# M-2 live-PG race test: concurrent acquire + interleaved update_last_request
# ---------------------------------------------------------------------------


@pytest.mark.live_pg
async def test_m2_race_live_pg_cooldown_interleave_is_observed(live_pg_dsn: str) -> None:
    """M-2 (live-PG): concurrent acquirer observes a cooldown set between its check and claim.

    This is the true concurrency invariant test: it uses a real asyncpg pool
    against a disposable PostgreSQL container so the FOR UPDATE + claim
    atomicity operates at the database level (not a mocked co-routine).

    Scenario
    --------
    1. Worker A calls ``_acquire_with_retry`` on a limiter with a very short
       min_interval (0.05s) so it wins the slot claim easily.
    2. Immediately after A has claimed the slot, the test calls
       ``update_last_request("rate_limit", retry_after_s=3600)`` to write a
       cooldown_until far in the future.
    3. Worker B then calls ``acquire()``.  Because the FOR UPDATE lock and the
       slot claim are in the SAME transaction, B's FOR UPDATE read sees the
       committed cooldown row and B sleeps through it rather than bypassing it.

    Old (broken) behaviour: step-1 read (FOR UPDATE) and step-2 claim were in
    separate transactions.  ``update_last_request`` could commit between them,
    so B might not observe the cooldown and proceed as if no cooldown existed.

    New (correct) behaviour: FOR UPDATE + INSERT are in one transaction.  The
    committed cooldown row is visible to B's FOR UPDATE read before B can
    attempt the INSERT — so B always sees the cooldown.

    Assertions
    ----------
    * ``is_in_cooldown()`` returns True before B runs.
    * B's ``acquire()`` sets ``cooldown_observed`` to True, meaning it slept
      (or returned without claiming) due to the active cooldown.
    """
    import asyncpg

    # Provision the schema — only source_health is needed.
    setup_conn = await asyncpg.connect(live_pg_dsn)
    try:
        await setup_conn.execute("""
            CREATE TABLE IF NOT EXISTS source_health (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NULL,
                source_type TEXT NOT NULL,
                last_request_at TIMESTAMPTZ,
                last_success_at TIMESTAMPTZ,
                last_status TEXT,
                cooldown_until TIMESTAMPTZ,
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT source_health_user_source
                    UNIQUE NULLS NOT DISTINCT (user_id, source_type)
            )
        """)
    finally:
        await setup_conn.close()

    pool = await asyncpg.create_pool(live_pg_dsn, min_size=2, max_size=4)
    try:
        source_type = f"test_race_{uuid.uuid4().hex[:8]}"
        limiter = PersistentSourceRateLimiter(
            source_type=source_type,
            user_id=None,
            min_interval_seconds=0.05,  # tiny so normal slot claim is instant
            db_pool=pool,
        )

        # Step 1: Worker A claims the slot (creates the row).
        await limiter._acquire_with_retry()

        # Step 2: Inject a "rate_limit" cooldown with a far-future expiry.
        # This simulates what happens when an HTTP 429 is received after A's
        # request: update_last_request runs on a separate connection/txn and
        # commits cooldown_until into the row.
        await limiter.update_last_request(status="rate_limit", retry_after_s=3600)

        # Verify the cooldown is now visible in the DB.
        in_cd, until = await limiter.is_in_cooldown()
        assert in_cd, "cooldown_until should be set after update_last_request('rate_limit')"
        assert until is not None

        # Step 3: Worker B tries to acquire.  With the fix, B's FOR UPDATE
        # read and slot claim are in ONE transaction.  The committed cooldown
        # row is locked and read atomically before the INSERT is even attempted
        # — so B MUST observe the cooldown and raise (causing acquire() to fall
        # back to a bare sleep), never claiming the slot.
        #
        # We intercept asyncio.sleep to prevent an actual 3600-second wait.
        sleep_called_with: list[float] = []

        async def _capturing_sleep(secs: float) -> None:
            sleep_called_with.append(secs)
            # Don't actually sleep — just record and return immediately.

        import unittest.mock as _mock

        with _mock.patch("asyncio.sleep", side_effect=_capturing_sleep):
            # acquire() uses the fallback (bare sleep) when _acquire_with_retry
            # raises, so we expect exactly one sleep call ≥ the cooldown remaining.
            await limiter.acquire()

        # B must have slept — cooldown was observed.
        assert len(sleep_called_with) >= 1, (
            "Worker B did not sleep — cooldown was bypassed (race window is open)"
        )
        # The sleep duration should be close to the remaining cooldown (~3600s),
        # proving B saw cooldown_until rather than proceeding through the slot claim.
        assert sleep_called_with[0] > 1000, (
            f"B's first sleep was only {sleep_called_with[0]:.1f}s — expected ~3600s "
            f"(cooldown); the limiter may have used the fallback min_interval sleep "
            f"instead of observing cooldown_until"
        )
    finally:
        await pool.close()


# ---------------------------------------------------------------------------
# D7-BLOCK: reset() and health_snapshot() — migrated from deleted B2 block
# (originally in test_source_rate_limiter.py, removed by D7 commit aebbefaf
# on the incorrect claim that test_persistent_source_rate_limiter.py "fully
# covers" these methods — it did not cover reset() or health_snapshot() at all)
# ---------------------------------------------------------------------------


async def test_reset_issues_upsert_clearing_cooldown_and_failures():
    """reset() UPSERTs last_status='ok', cooldown_until=NULL, failures=0."""
    pool, conn = _make_pool(fetchrow_side_effects=[None])
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


async def test_reset_swallows_db_error():
    """reset() never raises even when the DB pool errors."""
    pool, _ = _make_pool(raise_exc=OSError("db down"))
    limiter = PersistentSourceRateLimiter(
        source_type="arxiv",
        user_id=7,
        min_interval_seconds=3.0,
        db_pool=pool,
    )

    # Must not raise.
    await limiter.reset()


async def test_health_snapshot_in_cooldown():
    """Future cooldown_until → in_cooldown True, stale False."""
    until = datetime.now(tz=UTC) + timedelta(hours=1)
    last_req = datetime.now(tz=UTC) - timedelta(minutes=5)
    pool, _ = _make_pool(
        fetchrow_side_effects=[
            {
                "cooldown_until": until,
                "last_status": "rate_limit",
                "last_request_at": last_req,
            }
        ]
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


async def test_health_snapshot_stale_when_rate_limit_and_cooldown_expired():
    """rate_limit + past cooldown_until → stale True, in_cooldown False.

    This is exactly the stuck arXiv state from the bug report: a genuine 429
    set rate_limit + cooldown, the cooldown lapsed, nothing reset it.
    """
    past = datetime.now(tz=UTC) - timedelta(days=7)
    pool, _ = _make_pool(
        fetchrow_side_effects=[
            {
                "cooldown_until": past,
                "last_status": "rate_limit",
                "last_request_at": None,
            }
        ]
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


async def test_health_snapshot_stale_when_rate_limit_and_no_cooldown():
    """rate_limit + NULL cooldown_until → stale True (null-or-past rule)."""
    pool, _ = _make_pool(
        fetchrow_side_effects=[
            {
                "cooldown_until": None,
                "last_status": "rate_limit",
                "last_request_at": None,
            }
        ]
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


async def test_health_snapshot_fresh_ok_not_stale():
    """last_status='ok' is never stale and never in cooldown."""
    now = datetime.now(tz=UTC)
    pool, _ = _make_pool(
        fetchrow_side_effects=[
            {
                "cooldown_until": None,
                "last_status": "ok",
                "last_request_at": now,
            }
        ]
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


async def test_health_snapshot_no_row_returns_safe_default():
    """No source_health row → safe all-default snapshot."""
    pool, _ = _make_pool(fetchrow_side_effects=[None])
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


async def test_health_snapshot_db_error_returns_safe_default():
    """A DB error yields the safe default snapshot, never raises."""
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
