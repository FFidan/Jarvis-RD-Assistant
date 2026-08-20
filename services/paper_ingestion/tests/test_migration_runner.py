"""Tests for the database migration runner.

Tests rely on the local dev path resolution in run_migrations, which
resolves to the real db/migrations/ directory in the repo. This avoids
fragile monkeypatching of Path internals.
"""

import asyncpg
import pytest

from jarvis_common.migrations import required_code_schema
from jarvis_common.migrations import _strip_outer_transaction_control
from jarvis_common.testing_db import make_pool_and_conn


def _import_run_migrations():
    """Lazy import to avoid module-level import chain issues in test collection."""
    from jarvis_common.migrations import run_migrations

    return run_migrations


async def test_does_not_create_schema_migrations_table(tmp_path):
    """Only the SQL bootstrap/migration contract creates migration metadata."""
    run_migrations = _import_run_migrations()
    pool, conn = make_pool_and_conn()
    # An explicit empty test directory avoids applying packaged migrations.
    conn.fetch.return_value = []
    conn.fetchval.return_value = required_code_schema()  # Simulate a database at the floor.

    await run_migrations(pool, migrations_dir=tmp_path)

    all_calls = [str(c) for c in conn.execute.call_args_list]
    assert not any("CREATE TABLE" in c and "schema_migrations" in c for c in all_calls)


async def test_skips_already_applied_migrations(tmp_path):
    """Migrations already in schema_migrations should not be re-executed."""
    run_migrations = _import_run_migrations()
    pool, conn = make_pool_and_conn()
    # Post-squash: the baseline is pre-seeded; no .sql files on disk.
    conn.fetch.return_value = [{"version": v} for v in range(1, 102)]
    conn.fetchval.return_value = required_code_schema()  # Simulate a database at the floor.

    await run_migrations(pool, migrations_dir=tmp_path)

    # The outer transaction and advisory-lock savepoint are always opened.
    assert conn.transaction.call_count == 2


# test_applies_unapplied_migration — DELETED (db/migrations squash 2026-05-19):
# chain-coupled: used _real_migration_versions() which globs db/migrations/*.sql
# → returns [] post-squash, causing IndexError at versions[0]. No post-squash contract.

# test_applies_multiple_unapplied_in_order — DELETED (db/migrations squash 2026-05-19):
# chain-coupled: used _count_real_migrations() → 0 post-squash → expected_new = -10 FAIL.


async def test_no_migrations_applied_when_all_fresh(tmp_path):
    """When nothing is applied yet (fresh install), the runner applies zero SQL files.

    Post-squash contract: db/migrations/ has no .sql files (0089+ era, empty until next
    migration). init.sql pre-seeds schema_migrations 1..88; the runner is a no-op when
    files == {} ∩ applied == {}, i.e. no per-migration savepoint.
    """
    run_migrations = _import_run_migrations()
    pool, conn = make_pool_and_conn()
    conn.fetch.return_value = []  # Nothing applied yet
    conn.fetchval.return_value = required_code_schema()  # Simulate a database at the floor.

    await run_migrations(pool, migrations_dir=tmp_path)

    # Empty migrations_dir → outer transaction plus the advisory-lock savepoint.
    assert conn.transaction.call_count == 2


async def test_schema_migrations_select_called(tmp_path):
    """run_migrations should SELECT existing versions from schema_migrations."""
    run_migrations = _import_run_migrations()
    pool, conn = make_pool_and_conn()
    # Post-squash: no .sql files → nothing to probe; mock returns empty applied list.
    conn.fetch.return_value = []
    conn.fetchval.return_value = required_code_schema()  # Simulate a database at the floor.

    await run_migrations(pool, migrations_dir=tmp_path)

    conn.fetch.assert_any_await("SELECT version FROM schema_migrations")


async def test_migration_uses_xact_lock(tmp_path):
    """run_migrations must use pg_advisory_xact_lock (not session-level pg_advisory_lock)."""
    run_migrations = _import_run_migrations()
    pool, conn = make_pool_and_conn()
    # Post-squash: no .sql files; mock returns empty applied list.
    conn.fetch.return_value = []
    conn.fetchval.return_value = required_code_schema()  # Simulate a database at the floor.

    await run_migrations(pool, migrations_dir=tmp_path)

    execute_calls = [str(c) for c in conn.execute.call_args_list]
    # xact lock must be present
    assert any("pg_advisory_xact_lock" in c for c in execute_calls), (
        "Expected pg_advisory_xact_lock to be called"
    )
    # session-level lock must NOT be present
    assert not any("pg_advisory_lock(" in c and "xact" not in c for c in execute_calls), (
        "Session-level pg_advisory_lock must not be used"
    )


async def test_migration_lock_timeout_fails_closed_by_default(tmp_path):
    """Lock contention should fail startup unless compatibility mode is explicit."""
    run_migrations = _import_run_migrations()
    pool, conn = make_pool_and_conn()

    # Make execute raise LockNotAvailableError on the advisory lock call
    async def _execute_side_effect(sql, *_):
        if "pg_advisory_xact_lock" in sql:
            raise asyncpg.LockNotAvailableError()

    conn.execute.side_effect = _execute_side_effect

    with pytest.raises(RuntimeError, match="migration lock contended"):
        await run_migrations(pool, migrations_dir=tmp_path)

    # fetch (SELECT version FROM schema_migrations) must NOT have been called —
    # we bailed out before reaching it
    conn.fetch.assert_not_awaited()


async def test_migration_lock_timeout_rechecks_floor_with_env_flag(tmp_path, monkeypatch):
    """Compatibility mode starts only after the contending migrator reaches the floor."""
    run_migrations = _import_run_migrations()
    pool, conn = make_pool_and_conn()

    async def _execute_side_effect(sql, *_):
        if "pg_advisory_xact_lock" in sql:
            raise asyncpg.LockNotAvailableError()

    conn.execute.side_effect = _execute_side_effect
    conn.fetchval.return_value = required_code_schema()
    monkeypatch.setenv("JARVIS_MIGRATION_LOCK_CONTENDED_OK", "true")

    await run_migrations(pool, migrations_dir=tmp_path)

    conn.fetchval.assert_awaited_once_with(
        "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
    )
    conn.fetch.assert_not_awaited()


def test_migration_runner_strips_begin_commit():
    """Migration SQL with standalone BEGIN/COMMIT lines must have them stripped.

    DB-C01 regression: asyncpg wraps each migration in a savepoint transaction;
    nested explicit BEGIN/COMMIT commands cause "can't run BEGIN inside a
    transaction" errors.  The runner must strip them before executing.

    Uses the real production ``_strip_outer_transaction_control`` to verify
    correct behaviour (D4-10: prior version tested a local shadow copy).
    """
    sql_with_txn = (
        "BEGIN;\n"
        "CREATE TABLE IF NOT EXISTS test_strip (id int);\n"
        "ALTER TABLE test_strip ADD COLUMN val text;\n"
        "COMMIT;\n"
    )
    sql_with_trailing_semicolon = "BEGIN ;\nSELECT 1;\nCOMMIT ;\n"
    sql_with_rollback = "BEGIN\nSELECT 1;\nROLLBACK\n"

    result1 = _strip_outer_transaction_control(sql_with_txn)
    assert "BEGIN" not in result1
    assert "COMMIT" not in result1
    assert "CREATE TABLE IF NOT EXISTS test_strip" in result1
    assert "ALTER TABLE" in result1

    result2 = _strip_outer_transaction_control(sql_with_trailing_semicolon)
    assert "BEGIN" not in result2
    assert "COMMIT" not in result2
    assert "SELECT 1" in result2

    result3 = _strip_outer_transaction_control(sql_with_rollback)
    assert "BEGIN" not in result3
    assert "ROLLBACK" not in result3
    assert "SELECT 1" in result3


# test_migration_runner_strips_begin_commit_in_real_migrations — DELETED (db/migrations squash 2026-05-19):
# chain-coupled: globs db/migrations/*.sql → empty post-squash → cleaned_count == 0
# → assert cleaned_count > 0 FAIL. The pure-unit test above (test_migration_runner_strips_begin_commit)
# fully covers _strip_outer_transaction_control behavior for 0089+ migrations.
