"""Tests for the database migration runner.

Tests rely on the local dev path resolution in run_migrations, which
resolves to the real db/migrations/ directory in the repo. This avoids
fragile monkeypatching of Path internals.
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


# The real migrations directory in this repo
_MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "db" / "migrations"


def _make_pool_and_conn():
    """Create mock asyncpg Pool + Connection with transaction support."""
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
    """run_migrations should always create the schema_migrations table first."""
    run_migrations = _import_run_migrations()
    pool, conn = _make_pool_and_conn()
    total = _count_real_migrations()
    conn.fetch.return_value = [{"version": i} for i in range(1, total + 1)]

    await run_migrations(pool)

    first_execute_call = conn.execute.call_args_list[0]
    assert "CREATE TABLE IF NOT EXISTS schema_migrations" in str(first_execute_call)


async def test_skips_already_applied_migrations():
    """Migrations already in schema_migrations should not be re-executed."""
    run_migrations = _import_run_migrations()
    pool, conn = _make_pool_and_conn()
    total = _count_real_migrations()
    conn.fetch.return_value = [{"version": i} for i in range(1, total + 1)]

    await run_migrations(pool)

    # No migration SQL (via transaction) should have been executed.
    assert conn.transaction.call_count == 0


async def test_applies_unapplied_migration():
    """A migration not yet in schema_migrations should be executed in a transaction."""
    run_migrations = _import_run_migrations()
    pool, conn = _make_pool_and_conn()
    total = _count_real_migrations()
    # All applied except version 1
    conn.fetch.return_value = [{"version": i} for i in range(2, total + 1)]

    await run_migrations(pool)

    # Should have opened exactly one transaction (for version 1)
    assert conn.transaction.call_count == 1
    # The INSERT into schema_migrations should include version 1
    execute_calls = [str(c) for c in conn.execute.call_args_list]
    assert any("1" in c and "schema_migrations" in c for c in execute_calls[1:])


async def test_applies_multiple_unapplied_in_order():
    """Multiple unapplied migrations should be applied in numeric order."""
    run_migrations = _import_run_migrations()
    pool, conn = _make_pool_and_conn()
    # Only mark first 10 as applied; rest (11-16) should be applied
    conn.fetch.return_value = [{"version": i} for i in range(1, 11)]

    await run_migrations(pool)

    total = _count_real_migrations()
    expected_new = total - 10
    assert conn.transaction.call_count == expected_new


async def test_no_migrations_applied_when_all_fresh():
    """When nothing is applied yet, all migrations should run."""
    run_migrations = _import_run_migrations()
    pool, conn = _make_pool_and_conn()
    conn.fetch.return_value = []  # Nothing applied yet

    await run_migrations(pool)

    total = _count_real_migrations()
    assert conn.transaction.call_count == total


async def test_schema_migrations_select_called():
    """run_migrations should SELECT existing versions from schema_migrations."""
    run_migrations = _import_run_migrations()
    pool, conn = _make_pool_and_conn()
    total = _count_real_migrations()
    conn.fetch.return_value = [{"version": i} for i in range(1, total + 1)]

    await run_migrations(pool)

    conn.fetch.assert_awaited_once_with("SELECT version FROM schema_migrations")
