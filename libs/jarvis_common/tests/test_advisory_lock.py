"""Tests for AdvisoryLock and _kind_lock_key.

PR-A4: Postgres session-level advisory lock context manager.

All tests mock asyncpg.Pool/Connection to avoid a live DB dependency.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from jarvis_common.advisory_lock import AdvisoryLock, _kind_lock_key

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pool(*, try_lock_result: bool = True, raise_on_fetchrow: Exception | None = None):
    """Build a mock asyncpg.Pool with controlled pg_try_advisory_lock result."""
    conn = AsyncMock()

    if raise_on_fetchrow is not None:
        conn.fetchrow = AsyncMock(side_effect=raise_on_fetchrow)
    else:
        conn.fetchrow = AsyncMock(return_value={"got": try_lock_result})

    conn.execute = AsyncMock(return_value=None)

    pool = MagicMock()
    pool.acquire = AsyncMock(return_value=conn)
    pool.release = AsyncMock(return_value=None)
    return pool, conn


# ---------------------------------------------------------------------------
# test_advisory_lock_acquires_and_releases
# ---------------------------------------------------------------------------


async def test_advisory_lock_acquires_and_releases():
    """Lock is acquired, __aenter__ returns True, unlock called on __aexit__."""
    pool, conn = _make_pool(try_lock_result=True)

    lock = AdvisoryLock(pool, key1=12345, key2=0)

    async with lock as acquired:
        assert acquired is True
        # Connection should still be held
        pool.release.assert_not_called()

    # After exit: unlock should have been called, connection released
    conn.execute.assert_called_once()
    unlock_sql: str = conn.execute.call_args[0][0]
    assert "pg_advisory_unlock" in unlock_sql
    pool.release.assert_called_once_with(conn)


# ---------------------------------------------------------------------------
# test_second_lock_returns_false_while_first_held
# ---------------------------------------------------------------------------


async def test_second_lock_returns_false_while_first_held():
    """When pg_try_advisory_lock returns false, __aenter__ returns False."""
    pool, conn = _make_pool(try_lock_result=False)

    lock = AdvisoryLock(pool, key1=99, key2=1)

    async with lock as acquired:
        assert acquired is False

    # Lock was never acquired — pg_advisory_unlock must NOT be called
    conn.execute.assert_not_called()
    # Connection was released immediately after failed lock attempt
    pool.release.assert_called_once_with(conn)


# ---------------------------------------------------------------------------
# test_lock_released_on_exception_inside_with_block
# ---------------------------------------------------------------------------


async def test_lock_released_on_exception_inside_with_block():
    """Exception inside the `async with` block still releases lock + connection."""
    pool, conn = _make_pool(try_lock_result=True)

    lock = AdvisoryLock(pool, key1=42, key2=7)

    with pytest.raises(ValueError, match="boom"):
        async with lock as acquired:
            assert acquired is True
            raise ValueError("boom")

    # Unlock must have been called
    conn.execute.assert_called_once()
    assert "pg_advisory_unlock" in conn.execute.call_args[0][0]
    pool.release.assert_called_once_with(conn)


# ---------------------------------------------------------------------------
# test_aexit_is_noop_when_lock_not_acquired
# ---------------------------------------------------------------------------


async def test_aexit_noop_when_not_acquired():
    """__aexit__ is a no-op if the lock was not obtained (False return)."""
    pool, conn = _make_pool(try_lock_result=False)

    lock = AdvisoryLock(pool, key1=1, key2=0)

    async with lock as acquired:
        assert acquired is False

    # Only one pool.release call (the immediate release in __aenter__)
    assert pool.release.call_count == 1
    conn.execute.assert_not_called()


# ---------------------------------------------------------------------------
# test_connection_released_on_fetchrow_exception
# ---------------------------------------------------------------------------


async def test_connection_released_on_fetchrow_exception():
    """Pool connection is released even when fetchrow raises."""
    err = RuntimeError("pg timeout")
    pool, conn = _make_pool(raise_on_fetchrow=err)

    lock = AdvisoryLock(pool, key1=5, key2=0)

    with pytest.raises(RuntimeError, match="pg timeout"):
        async with lock:
            pass  # should not reach here

    # Connection must have been released
    pool.release.assert_called_once_with(conn)
    # Unlock must NOT have been called
    conn.execute.assert_not_called()


# ---------------------------------------------------------------------------
# _kind_lock_key tests
# ---------------------------------------------------------------------------


def test_kind_lock_key_is_non_negative():
    """_kind_lock_key always returns a non-negative 32-bit integer."""
    for kind in ["arxiv", "s2", "pubmed", "openalex", "", "x" * 200]:
        result = _kind_lock_key(kind)
        assert 0 <= result <= 0x7FFF_FFFF, f"Out of range for {kind!r}: {result}"


def test_kind_lock_key_stable_across_pythonhashseed():
    """Key matches the SHA-256-derived formula — not Python's hash(), so PYTHONHASHSEED has no effect.

    This verifies the algorithm is SHA-256-based (which is deterministic by definition).
    True cross-seed independence is structural: the implementation never calls hash().
    """
    import hashlib

    kind = "pulse"
    expected = int.from_bytes(hashlib.sha256(kind.encode()).digest()[:4], "big") & 0x7FFF_FFFF
    assert _kind_lock_key(kind) == expected


def test_kind_lock_key_known_value_digest():
    """Pin a known hash to catch accidental algorithm changes."""
    import hashlib

    kind = "digest"
    expected = int.from_bytes(hashlib.sha256(kind.encode()).digest()[:4], "big") & 0x7FFF_FFFF
    assert _kind_lock_key(kind) == expected
