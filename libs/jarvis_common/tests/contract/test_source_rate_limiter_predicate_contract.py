"""Predicate-direct contract tests for PersistentSourceRateLimiter (A262).

Tests exercise the public surface of PersistentSourceRateLimiter against a
real Postgres schema via SharedConnPool(contract_conn). Each test uses a
unique source_type to avoid row collisions; the outer transaction is rolled
back after each test so no state leaks.

Verified: libs/jarvis_common/jarvis_common/source_rate_limiter.py:74-492 at HEAD.
Survivor-of an earlier consolidation: per-source mock unit tests replaced by this predicate-direct suite.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from jarvis_common.source_rate_limiter import SourceRateLimiter
from jarvis_common.testing import SharedConnPool

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def _research_runtime_identity(contract_conn):
    """Run each limiter contract under the real Research runtime identity."""
    await contract_conn.execute("SET LOCAL SESSION AUTHORIZATION jarvis_research_runtime")


def _make_limiter(
    conn,
    source_type: str,
    min_interval_seconds: float = 0.0,
    fallback: SourceRateLimiter | None = None,
):
    """Build a PersistentSourceRateLimiter backed by SharedConnPool(conn)."""
    from jarvis_common.source_rate_limiter import PersistentSourceRateLimiter

    pool = SharedConnPool(conn)
    return PersistentSourceRateLimiter(
        source_type=source_type,
        user_id=None,
        min_interval_seconds=min_interval_seconds,
        db_pool=pool,
        fallback=fallback,
    )


class _CountingFallback(SourceRateLimiter):
    """In-memory fallback spy: counts acquire() calls, delegates to a real bucket."""

    def __init__(self) -> None:
        super().__init__(rate_per_second=1000.0, burst=10)
        self.calls = 0

    async def acquire(self) -> None:
        self.calls += 1
        await super().acquire()


async def _seed_cooldown_row(
    conn,
    source_type: str,
    *,
    last_request_at: datetime,
    cooldown_until: datetime,
) -> None:
    """Insert a source_health row in rate_limit state with an explicit cooldown."""
    await conn.execute(
        """
        INSERT INTO source_health
            (user_id, source_type, last_request_at, last_status, cooldown_until)
        VALUES (NULL, $1, $2, 'rate_limit', $3)
        """,
        source_type,
        last_request_at,
        cooldown_until,
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


async def test_a262_cooldown_after_error_blocks_acquire(contract_conn):
    """A262: after update_last_request('error') increments consecutive_failures.

    A plain 'error' outcome does NOT set cooldown_until — consecutive_failures
    accumulates. This test verifies the error branch increments the counter and
    that is_in_cooldown() remains False until an explicit 'rate_limit' update.

    Verified: source_rate_limiter.py:318-338 — error branch increments
    consecutive_failures; cooldown_until not set.
    Supersedes: mock-unit test_source_rate_limiter.py::test_error_increments_failures.
    """
    source_type = f"contract_rl_err_{uuid.uuid4().hex[:8]}"
    limiter = _make_limiter(contract_conn, source_type, min_interval_seconds=0.0)

    await limiter.acquire()
    await limiter.update_last_request("error")

    # After 'error' alone: NOT in cooldown
    in_cd, until = await limiter.is_in_cooldown()
    assert in_cd is False
    assert until is None

    row = await contract_conn.fetchrow(
        "SELECT consecutive_failures, cooldown_until FROM source_health "
        "WHERE source_type = $1 AND user_id IS NULL",
        source_type,
    )
    assert row is not None
    assert row["consecutive_failures"] >= 1, "error should increment consecutive_failures"
    assert row["cooldown_until"] is None, "error alone must not set cooldown_until"


async def test_a262_cooldown_persists_in_db(contract_conn):
    """A262: cooldown_until survives re-read by a second limiter instance.

    Simulates a process restart: a fresh PersistentSourceRateLimiter pointing at
    the same source_type still sees the cooldown written by the first instance.

    Verified: source_rate_limiter.py:347-382 — is_in_cooldown reads from DB
    each time; no in-memory state carried between instances.
    Supersedes: mock-unit test_source_rate_limiter.py::test_cooldown_survives_restart.
    """
    source_type = f"contract_rl_persist_{uuid.uuid4().hex[:8]}"
    limiter1 = _make_limiter(contract_conn, source_type, min_interval_seconds=0.0)

    await limiter1.acquire()
    await limiter1.update_last_request("rate_limit", retry_after_s=3600)

    # "restart": create a brand-new limiter pointing at the same row
    limiter2 = _make_limiter(contract_conn, source_type, min_interval_seconds=0.0)
    in_cd, until = await limiter2.is_in_cooldown()

    assert in_cd is True, "New limiter instance should still see the DB cooldown"
    assert until is not None
    assert until > datetime.now(tz=UTC)


async def test_a262_ok_update_clears_cooldown_and_failures(contract_conn):
    """A262: update_last_request('ok') clears cooldown_until and resets consecutive_failures.

    Verified: source_rate_limiter.py:274-292 — 'ok' branch sets cooldown_until=NULL,
    consecutive_failures=0.
    Supersedes: mock-unit test_source_rate_limiter.py::test_ok_clears_state.
    """
    source_type = f"contract_rl_ok_{uuid.uuid4().hex[:8]}"
    limiter = _make_limiter(contract_conn, source_type, min_interval_seconds=0.0)

    await limiter.acquire()
    await limiter.update_last_request("error")
    await limiter.update_last_request("rate_limit", retry_after_s=3600)

    # Confirm both error state and cooldown are set
    row_before = await contract_conn.fetchrow(
        "SELECT cooldown_until, consecutive_failures FROM source_health "
        "WHERE source_type = $1 AND user_id IS NULL",
        source_type,
    )
    assert row_before is not None
    assert row_before["cooldown_until"] is not None

    await limiter.update_last_request("ok")

    in_cd, _ = await limiter.is_in_cooldown()
    assert in_cd is False

    row_after = await contract_conn.fetchrow(
        "SELECT cooldown_until, consecutive_failures, last_status FROM source_health "
        "WHERE source_type = $1 AND user_id IS NULL",
        source_type,
    )
    assert row_after is not None
    assert row_after["cooldown_until"] is None
    assert row_after["consecutive_failures"] == 0
    assert row_after["last_status"] == "ok"


# ---------------------------------------------------------------------------
# M2 (audit 2026-06-10): post-cooldown bounded re-claim — no burst, no silent
# skip, no unbounded loop under a re-armed cooldown.
# ---------------------------------------------------------------------------


async def test_m2_concurrent_cooldown_acquires_one_claims_rest_throttled(contract_conn):
    """M2: N concurrent acquire()s during an active cooldown — exactly one claims.

    After the cooldown elapses exactly one caller claims the slot (writes
    last_request_at) and every other caller is throttled (routed through the
    fallback limiter after the bounded claim retries). No caller may return
    having neither claimed nor been throttled.

    Defect (pre-fix): the cooldown branch slept then RETURNED without ever
    re-attempting the claim, so all N callers burst through together at
    cooldown expiry and last_request_at stayed stale (nobody claimed).

    Harness note: inside the contract rollback transaction PG ``now()`` is
    frozen at txn start, so after the first claim in a test no later claim can
    succeed — the losers deterministically exhaust the claim budget and end at
    the documented RuntimeError, which public acquire() turns into a fallback
    acquire. That makes "exactly one claims, the rest are throttled" exact.

    # Verified: source_rate_limiter.py:229-236 — cooldown sleep path under test.
    """
    source_type = f"contract_rl_m2_burst_{uuid.uuid4().hex[:8]}"
    cooldown_s = 0.4
    fallback = _CountingFallback()
    limiter = _make_limiter(contract_conn, source_type, min_interval_seconds=0.8, fallback=fallback)

    now = datetime.now(tz=UTC)
    seeded_last_request = now - timedelta(seconds=60)
    await _seed_cooldown_row(
        contract_conn,
        source_type,
        last_request_at=seeded_last_request,
        cooldown_until=now + timedelta(seconds=cooldown_s),
    )

    start = time.monotonic()
    elapsed: list[float] = []

    async def _worker() -> None:
        await limiter.acquire()
        elapsed.append(time.monotonic() - start)

    await asyncio.gather(_worker(), _worker(), _worker())

    # (a) exactly one caller claimed the slot: last_request_at advanced past the seed.
    row = await contract_conn.fetchrow(
        "SELECT last_request_at FROM source_health WHERE source_type = $1 AND user_id IS NULL",
        source_type,
    )
    assert row is not None
    assert row["last_request_at"] > seeded_last_request, (
        "no caller ever claimed the slot after the cooldown elapsed (cooldown burst / silent skip)"
    )

    # (b) the two losers were throttled via the fallback — never a silent skip.
    assert fallback.calls == 2, (
        f"expected the 2 non-claiming callers to be throttled via the fallback, "
        f"got {fallback.calls} fallback acquires"
    )

    # (c) nobody burst through the active cooldown without waiting it out.
    assert len(elapsed) == 3
    assert all(e >= 0.75 * cooldown_s for e in elapsed), (
        f"a caller returned before the cooldown elapsed: {elapsed}"
    )


async def test_m2_rearmed_cooldown_terminates_in_bounded_raise(contract_conn, monkeypatch):
    """M2: a cooldown still active after the one allowed cooldown sleep must end
    in the documented RuntimeError after a bounded number of sleeps — never an
    unbounded loop and never a bare return without claiming.

    asyncio.sleep is patched to return instantly, so the far-future cooldown is
    still active when the loop re-attempts — equivalent to a continuously
    re-armed cooldown.

    # Verified: source_rate_limiter.py:259-262 — RuntimeError fallback raise.
    """
    sleeps: list[float] = []

    async def _instant_sleep(secs: float) -> None:
        sleeps.append(secs)

    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)

    source_type = f"contract_rl_m2_rearm_{uuid.uuid4().hex[:8]}"
    limiter = _make_limiter(contract_conn, source_type, min_interval_seconds=0.05)

    now = datetime.now(tz=UTC)
    await _seed_cooldown_row(
        contract_conn,
        source_type,
        last_request_at=now - timedelta(seconds=60),
        cooldown_until=now + timedelta(seconds=30),
    )

    # asyncio.timeout converts a hypothetical unbounded loop (which still hits
    # the DB and therefore yields) into TimeoutError instead of a hung test.
    async with asyncio.timeout(10):
        with pytest.raises(RuntimeError, match="rate-limit enforced via fallback"):
            await limiter._acquire_with_retry()

    # Bounded: exactly ONE cooldown sleep (the ~30s one), then the raise.
    assert len(sleeps) == 1, f"expected exactly one cooldown sleep, got {sleeps}"
    assert sleeps[0] > 1.0


async def test_m2_rearmed_cooldown_public_acquire_terminates_via_fallback(
    contract_conn, monkeypatch
):
    """M2: public acquire() under a permanently re-armed cooldown terminates and
    enforces the rate limit via the fallback limiter.

    Pre-fix it returned after one cooldown sleep WITHOUT claiming or falling
    back — a silent skip of the post-cooldown claim.

    # Verified: source_rate_limiter.py:128-140 — acquire() fallback on raise.
    """
    sleeps: list[float] = []

    async def _instant_sleep(secs: float) -> None:
        sleeps.append(secs)

    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)

    source_type = f"contract_rl_m2_pub_{uuid.uuid4().hex[:8]}"
    fallback = _CountingFallback()
    limiter = _make_limiter(
        contract_conn, source_type, min_interval_seconds=0.05, fallback=fallback
    )

    now = datetime.now(tz=UTC)
    await _seed_cooldown_row(
        contract_conn,
        source_type,
        last_request_at=now - timedelta(seconds=60),
        cooldown_until=now + timedelta(seconds=30),
    )

    async with asyncio.timeout(10):
        await limiter.acquire()

    assert fallback.calls == 1, (
        "acquire() under a re-armed cooldown must enforce the rate limit via "
        f"the fallback, got {fallback.calls} fallback acquires (silent skip)"
    )
