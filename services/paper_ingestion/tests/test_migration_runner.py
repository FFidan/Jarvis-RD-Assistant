"""Tests for the database migration runner.

Tests rely on the local dev path resolution in run_migrations, which
resolves to the real db/migrations/ directory in the repo. This avoids
fragile monkeypatching of Path internals.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import asyncpg

# The real migrations directory in this repo
_MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "db" / "migrations"


def _make_pool_and_conn():
    """Create mock asyncpg Pool + Connection with transaction support.

    The outer transaction (wraps the advisory lock) and each per-migration
    transaction are all handled by the same reusable transaction context
    manager mock.
    """
    conn = AsyncMock()
    txn_cm = MagicMock()
    txn_cm.__aenter__ = AsyncMock(return_value=txn_cm)
    txn_cm.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn_cm)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx
    return pool, conn


def _count_real_migrations() -> int:
    """Count the number of .sql migration files with numeric prefixes."""
    count = 0
    for f in _MIGRATIONS_DIR.glob("*.sql"):
        try:
            int(f.name.split("_")[0])
            count += 1
        except (ValueError, IndexError):
            pass
    return count


def _import_run_migrations():
    """Lazy import to avoid module-level import chain issues with Docker deps."""
    from app.main import run_migrations

    return run_migrations


async def test_creates_schema_migrations_table():
    """run_migrations should create the schema_migrations table after acquiring the lock."""
    run_migrations = _import_run_migrations()
    pool, conn = _make_pool_and_conn()
    total = _count_real_migrations()
    conn.fetch.return_value = [{"version": i} for i in range(1, total + 1)]

    await run_migrations(pool)

    # First execute: SET LOCAL lock_timeout
    # Second execute: SELECT pg_advisory_xact_lock(42)
    # Third execute: CREATE TABLE IF NOT EXISTS schema_migrations
    all_calls = [str(c) for c in conn.execute.call_args_list]
    assert any("CREATE TABLE IF NOT EXISTS schema_migrations" in c for c in all_calls)


async def test_skips_already_applied_migrations():
    """Migrations already in schema_migrations should not be re-executed."""
    run_migrations = _import_run_migrations()
    pool, conn = _make_pool_and_conn()
    total = _count_real_migrations()
    conn.fetch.return_value = [{"version": i} for i in range(1, total + 1)]

    await run_migrations(pool)

    # Only the outer wrapping transaction should have been opened (no migration transactions).
    assert conn.transaction.call_count == 1


async def test_applies_unapplied_migration():
    """A migration not yet in schema_migrations should be executed in a transaction."""
    run_migrations = _import_run_migrations()
    pool, conn = _make_pool_and_conn()
    total = _count_real_migrations()
    # All applied except version 1
    conn.fetch.return_value = [{"version": i} for i in range(2, total + 1)]

    await run_migrations(pool)

    # Outer transaction (1) + one migration transaction (1) = 2
    assert conn.transaction.call_count == 2
    # The INSERT into schema_migrations should include version 1
    execute_calls = [str(c) for c in conn.execute.call_args_list]
    assert any("1" in c and "schema_migrations" in c for c in execute_calls[1:])


async def test_applies_multiple_unapplied_in_order():
    """Multiple unapplied migrations should be applied in numeric order."""
    run_migrations = _import_run_migrations()
    pool, conn = _make_pool_and_conn()
    # Only mark first 10 as applied; rest should be applied
    conn.fetch.return_value = [{"version": i} for i in range(1, 11)]

    await run_migrations(pool)

    total = _count_real_migrations()
    expected_new = total - 10
    # Outer transaction (1) + one transaction per new migration
    assert conn.transaction.call_count == expected_new + 1


async def test_no_migrations_applied_when_all_fresh():
    """When nothing is applied yet, all migrations should run."""
    run_migrations = _import_run_migrations()
    pool, conn = _make_pool_and_conn()
    conn.fetch.return_value = []  # Nothing applied yet

    await run_migrations(pool)

    total = _count_real_migrations()
    # Outer transaction (1) + one transaction per migration
    assert conn.transaction.call_count == total + 1


async def test_schema_migrations_select_called():
    """run_migrations should SELECT existing versions from schema_migrations."""
    run_migrations = _import_run_migrations()
    pool, conn = _make_pool_and_conn()
    total = _count_real_migrations()
    conn.fetch.return_value = [{"version": i} for i in range(1, total + 1)]

    await run_migrations(pool)

    conn.fetch.assert_awaited_once_with("SELECT version FROM schema_migrations")


async def test_migration_uses_xact_lock():
    """run_migrations must use pg_advisory_xact_lock (not session-level pg_advisory_lock)."""
    run_migrations = _import_run_migrations()
    pool, conn = _make_pool_and_conn()
    total = _count_real_migrations()
    conn.fetch.return_value = [{"version": i} for i in range(1, total + 1)]

    await run_migrations(pool)

    execute_calls = [str(c) for c in conn.execute.call_args_list]
    # xact lock must be present
    assert any("pg_advisory_xact_lock" in c for c in execute_calls), (
        "Expected pg_advisory_xact_lock to be called"
    )
    # session-level lock must NOT be present
    assert not any("pg_advisory_lock(" in c and "xact" not in c for c in execute_calls), (
        "Session-level pg_advisory_lock must not be used"
    )


async def test_migration_lock_timeout_returns_gracefully():
    """LockNotAvailableError from pg_advisory_xact_lock causes run_migrations to return
    gracefully without raising."""
    run_migrations = _import_run_migrations()
    pool, conn = _make_pool_and_conn()

    # Make execute raise LockNotAvailableError on the advisory lock call
    async def _execute_side_effect(sql, *_):
        if "pg_advisory_xact_lock" in sql:
            raise asyncpg.LockNotAvailableError()

    conn.execute.side_effect = _execute_side_effect

    # Must not raise — returns gracefully, letting the other instance run migrations
    await run_migrations(pool)

    # fetch (SELECT version FROM schema_migrations) must NOT have been called —
    # we bailed out before reaching it
    conn.fetch.assert_not_awaited()
