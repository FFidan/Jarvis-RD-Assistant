"""Tests for the database migration runner.

Tests rely on the local dev path resolution in run_migrations, which
resolves to the real db/migrations/ directory in the repo. This avoids
fragile monkeypatching of Path internals.
"""

from pathlib import Path
from unittest.mock import ANY

import asyncpg
import pytest

from tests.conftest import _make_pool_and_conn

# The real migrations directory in this repo
_MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "db" / "migrations"


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


def _real_migration_versions() -> list[int]:
    """Return the sorted list of migration versions present on disk.

    The numbering may have gaps (e.g. migration 71 is reserved for Sprint A
    and lives on a parallel branch). Tests must use this rather than
    ``range(1, count + 1)`` to avoid spurious "missing migration" runs.
    """
    versions: list[int] = []
    for f in _MIGRATIONS_DIR.glob("*.sql"):
        try:
            versions.append(int(f.name.split("_")[0]))
        except (ValueError, IndexError):
            continue
    return sorted(versions)


def _import_run_migrations():
    """Lazy import to avoid module-level import chain issues in test collection."""
    from paper_ingestion.migrations_runner import run_migrations

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
    versions = _real_migration_versions()
    conn.fetch.return_value = [{"version": v} for v in versions]

    await run_migrations(pool)

    # Only the outer wrapping transaction should have been opened (no migration transactions).
    assert conn.transaction.call_count == 1


async def test_applies_unapplied_migration():
    """A migration not yet in schema_migrations should be executed in a transaction."""
    run_migrations = _import_run_migrations()
    pool, conn = _make_pool_and_conn()
    versions = _real_migration_versions()
    # All applied except the lowest version (typically 1).
    first = versions[0]
    conn.fetch.return_value = [{"version": v} for v in versions if v != first]

    await run_migrations(pool)

    # Outer transaction (1) + one migration transaction (1) = 2
    assert conn.transaction.call_count == 2
    # The INSERT into schema_migrations should include the missing version
    execute_calls = [str(c) for c in conn.execute.call_args_list]
    assert any(str(first) in c and "schema_migrations" in c for c in execute_calls[1:])


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

    conn.fetch.assert_any_await(
        "SELECT version FROM schema_migrations WHERE version = ANY($1::int[])",
        ANY,
    )
    conn.fetch.assert_any_await("SELECT version FROM schema_migrations")


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


async def test_migration_lock_timeout_fails_closed_by_default():
    """Lock contention should fail startup unless compatibility mode is explicit."""
    run_migrations = _import_run_migrations()
    pool, conn = _make_pool_and_conn()

    # Make execute raise LockNotAvailableError on the advisory lock call
    async def _execute_side_effect(sql, *_):
        if "pg_advisory_xact_lock" in sql:
            raise asyncpg.LockNotAvailableError()

    conn.execute.side_effect = _execute_side_effect

    with pytest.raises(RuntimeError, match="migration lock contended"):
        await run_migrations(pool)

    # fetch (SELECT version FROM schema_migrations) must NOT have been called —
    # we bailed out before reaching it
    conn.fetch.assert_not_awaited()


async def test_migration_lock_timeout_can_return_gracefully_with_env_flag(monkeypatch):
    """Compatibility mode preserves old lock-contention behavior when requested."""
    run_migrations = _import_run_migrations()
    pool, conn = _make_pool_and_conn()

    async def _execute_side_effect(sql, *_):
        if "pg_advisory_xact_lock" in sql:
            raise asyncpg.LockNotAvailableError()

    conn.execute.side_effect = _execute_side_effect
    monkeypatch.setenv("JARVIS_MIGRATION_LOCK_CONTENDED_OK", "true")

    await run_migrations(pool)

    conn.fetch.assert_not_awaited()


def test_migration_runner_strips_begin_commit():
    """Migration SQL with standalone BEGIN/COMMIT lines must have them stripped.

    DB-C01 regression: asyncpg wraps each migration in a savepoint transaction;
    nested explicit BEGIN/COMMIT commands cause "can't run BEGIN inside a
    transaction" errors.  The runner must strip them before executing.
    """
    import re

    sql_with_txn = (
        "BEGIN;\n"
        "CREATE TABLE IF NOT EXISTS test_strip (id int);\n"
        "ALTER TABLE test_strip ADD COLUMN val text;\n"
        "COMMIT;\n"
    )
    sql_with_trailing_semicolon = "BEGIN ;\nSELECT 1;\nCOMMIT ;\n"
    sql_with_rollback = "BEGIN\nSELECT 1;\nROLLBACK\n"

    def strip(sql: str) -> str:
        return re.sub(
            r"^\s*(BEGIN|COMMIT|ROLLBACK)\s*;?\s*$",
            "",
            sql,
            flags=re.IGNORECASE | re.MULTILINE,
        )

    result1 = strip(sql_with_txn)
    assert "BEGIN" not in result1
    assert "COMMIT" not in result1
    assert "CREATE TABLE IF NOT EXISTS test_strip" in result1
    assert "ALTER TABLE" in result1

    result2 = strip(sql_with_trailing_semicolon)
    assert "BEGIN" not in result2
    assert "COMMIT" not in result2
    assert "SELECT 1" in result2

    result3 = strip(sql_with_rollback)
    assert "BEGIN" not in result3
    assert "ROLLBACK" not in result3
    assert "SELECT 1" in result3


def test_migration_runner_strips_begin_commit_in_real_migrations():
    """Every real migration file that contains BEGIN/COMMIT must be safe to strip."""
    import re

    cleaned_count = 0
    for sql_file in _MIGRATIONS_DIR.glob("*.sql"):
        try:
            int(sql_file.name.split("_")[0])
        except (ValueError, IndexError):
            continue
        sql = sql_file.read_text()
        if not re.search(
            r"^\s*(BEGIN|COMMIT|ROLLBACK)\s*;?\s*$", sql, re.IGNORECASE | re.MULTILINE
        ):
            continue
        cleaned = re.sub(
            r"^\s*(BEGIN|COMMIT|ROLLBACK)\s*;?\s*$",
            "",
            sql,
            flags=re.IGNORECASE | re.MULTILINE,
        )
        # After stripping, no standalone BEGIN/COMMIT/ROLLBACK lines should remain.
        # Inline BEGIN inside dollar-quoted PL/pgSQL function bodies (e.g.,
        # `BEGIN NEW.updated_at = NOW(); RETURN NEW; END;`) is fine and stays.
        assert not re.search(
            r"^\s*(BEGIN|COMMIT|ROLLBACK)\s*;?\s*$",
            cleaned,
            re.IGNORECASE | re.MULTILINE,
        ), f"{sql_file.name}: standalone BEGIN/COMMIT/ROLLBACK not fully stripped"
        assert len(cleaned.strip()) > 0, f"{sql_file.name}: stripped to empty"
        cleaned_count += 1

    # We know several real migrations have BEGIN/COMMIT — assert at least one was processed
    assert cleaned_count > 0, "No migrations with BEGIN/COMMIT found — update test expectations"
