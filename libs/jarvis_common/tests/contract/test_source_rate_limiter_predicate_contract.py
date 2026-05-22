"""Predicate-direct contract tests for PersistentSourceRateLimiter (A262).

Tests exercise the public surface of PersistentSourceRateLimiter against a
real Postgres schema via SharedConnPool(contract_conn). Each test uses a
unique source_type to avoid row collisions; the outer transaction is rolled
back after each test so no state leaks.

Verified: libs/jarvis_common/jarvis_common/source_rate_limiter.py:74-492 at HEAD.
Survivor-of (Phase C): per-source mock unit tests replaced by this predicate-direct suite.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from jarvis_common.testing import SharedConnPool

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]


def _make_limiter(conn, source_type: str, min_interval_seconds: float = 0.0):
    """Build a PersistentSourceRateLimiter backed by SharedConnPool(conn)."""
    from jarvis_common.source_rate_limiter import PersistentSourceRateLimiter

    pool = SharedConnPool(conn)
    return PersistentSourceRateLimiter(
        source_type=source_type,
        user_id=None,
        min_interval_seconds=min_interval_seconds,
        db_pool=pool,
    )


async def test_a262_acquire_inserts_source_health_row(contract_conn):
    """A262: first acquire() inserts a source_health row for the source_type.

    Verified: source_rate_limiter.py:164-172 — INSERT INTO source_health … RETURNING.
    """
    source_type = f"contract_rl_first_{uuid.uuid4().hex[:8]}"
    limiter = _make_limiter(contract_conn, source_type, min_interval_seconds=0.0)

    await limiter.acquire()

    row = await contract_conn.fetchrow(
        "SELECT source_type, last_request_at "
        "FROM source_health WHERE source_type = $1 AND user_id IS NULL",
        source_type,
    )
    assert row is not None, "acquire() did not insert a source_health row"
    assert row["last_request_at"] is not None


async def test_a262_is_in_cooldown_false_after_normal_acquire(contract_conn):
    """A262: after a normal acquire(), is_in_cooldown() returns (False, None).

    Verified: source_rate_limiter.py:336-378 — cooldown_until IS NULL → not in cooldown.
    """
    source_type = f"contract_rl_nocd_{uuid.uuid4().hex[:8]}"
    limiter = _make_limiter(contract_conn, source_type, min_interval_seconds=0.0)

    await limiter.acquire()
    in_cooldown, until = await limiter.is_in_cooldown()

    assert in_cooldown is False
    assert until is None


async def test_a262_update_last_request_rate_limit_sets_cooldown(contract_conn):
    """A262: update_last_request('rate_limit') sets a future cooldown_until row.

    Verified: source_rate_limiter.py:271-301 — INSERT … ON CONFLICT … cooldown_until = $5.
    """
    source_type = f"contract_rl_cd_{uuid.uuid4().hex[:8]}"
    limiter = _make_limiter(contract_conn, source_type, min_interval_seconds=0.0)

    await limiter.acquire()
    await limiter.update_last_request("rate_limit", retry_after_s=3600)

    in_cooldown, until = await limiter.is_in_cooldown()
    assert in_cooldown is True, "Expected cooldown to be active after rate_limit update"
    assert until is not None
    assert until > datetime.now(tz=UTC)


async def test_a262_reset_clears_cooldown(contract_conn):
    """A262: reset() clears cooldown_until and sets last_status='ok'.

    Verified: source_rate_limiter.py:381-423 — DO UPDATE SET cooldown_until = NULL.
    """
    source_type = f"contract_rl_reset_{uuid.uuid4().hex[:8]}"
    limiter = _make_limiter(contract_conn, source_type, min_interval_seconds=0.0)

    await limiter.acquire()
    await limiter.update_last_request("rate_limit", retry_after_s=3600)

    # Confirm cooldown is set
    in_cd_before, _ = await limiter.is_in_cooldown()
    assert in_cd_before is True, "Pre-condition: cooldown should be active"

    await limiter.reset()

    in_cd_after, _ = await limiter.is_in_cooldown()
    assert in_cd_after is False, "reset() should clear cooldown_until"

    row = await contract_conn.fetchrow(
        "SELECT last_status FROM source_health WHERE source_type = $1 AND user_id IS NULL",
        source_type,
    )
    assert row is not None
    assert row["last_status"] == "ok"


async def test_a262_window_reset_via_sql_update(contract_conn):
    """A262: setting last_request_at into the past via SQL UPDATE allows re-acquire.

    This is the canonical 'window reset' pattern: directly manipulate the timestamp
    rather than sleeping, then verify acquire() claims the slot again.

    Verified: source_rate_limiter.py:164-172 — WHERE last_request_at < now() - interval.
    """
    source_type = f"contract_rl_win_{uuid.uuid4().hex[:8]}"
    # Set a 1-second minimum interval
    limiter = _make_limiter(contract_conn, source_type, min_interval_seconds=1.0)

    # Claim the slot
    await limiter.acquire()

    # Push last_request_at 2 seconds into the past so the window has expired
    past = datetime.now(tz=UTC) - timedelta(seconds=2)
    await contract_conn.execute(
        "UPDATE source_health SET last_request_at = $1 WHERE source_type = $2 AND user_id IS NULL",
        past,
        source_type,
    )

    # acquire() should now claim the slot again (no sleep needed)
    await limiter.acquire()

    row = await contract_conn.fetchrow(
        "SELECT last_request_at FROM source_health WHERE source_type = $1 AND user_id IS NULL",
        source_type,
    )
    assert row is not None
    # last_request_at should have been updated to approximately now
    assert row["last_request_at"] > past
