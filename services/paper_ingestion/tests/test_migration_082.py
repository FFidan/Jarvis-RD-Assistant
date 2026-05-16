"""Structural and live-PG tests for migration 082 (user_id FK gap — H-3).

Migration 082 adds ``<table>_user_id_fkey`` constraints on 7 tables that
migrations 077/080 missed: pulse_decks, recommendation_feedback,
source_health, source_run_history, daily_intent, journal_entries
(ON DELETE CASCADE) and pulse_models (ON DELETE SET NULL).
Text-based assertions verify the SQL artifact; the live-PG test exercises
a fresh apply and confirms FK existence plus zero orphan rows.
"""

from __future__ import annotations

from pathlib import Path

import asyncpg
import pytest

MIGRATION = Path(__file__).resolve().parents[3] / "db/migrations/082_user_id_fk_gap.sql"

# 6 tables whose FK uses ON DELETE CASCADE (owned-data rows).
CASCADE_TABLES: tuple[str, ...] = (
    "pulse_decks",
    "recommendation_feedback",
    "source_health",
    "source_run_history",
    "daily_intent",
    "journal_entries",
)

# 1 table whose FK uses ON DELETE SET NULL (NULL = shared/system model).
SET_NULL_TABLES: tuple[str, ...] = ("pulse_models",)

ALL_TABLES: tuple[str, ...] = CASCADE_TABLES + SET_NULL_TABLES


# ---------------------------------------------------------------------------
# Text-based correctness — always runs (no DB dependency).
# ---------------------------------------------------------------------------


def test_migration_082_file_exists() -> None:
    assert MIGRATION.is_file(), f"Missing migration file: {MIGRATION}"


def test_migration_082_covers_all_7_tables() -> None:
    """Every expected table must appear with: orphan UPDATE, DROP CONSTRAINT IF EXISTS,
    ADD CONSTRAINT <table>_user_id_fkey REFERENCES users, wrapped in the canonical
    idempotent DO $$ … EXCEPTION WHEN duplicate_object guard (migration 051 pattern).
    """
    sql = MIGRATION.read_text(encoding="utf-8")

    for table in ALL_TABLES:
        # 1. Orphan NULL-out.
        assert f"UPDATE {table}" in sql, f"missing orphan UPDATE for {table}"
        # 2. Idempotent DROP.
        assert f"DROP CONSTRAINT IF EXISTS {table}_user_id_fkey" in sql, (
            f"missing idempotent DROP for {table}"
        )
        # 3. ADD CONSTRAINT.
        assert f"ADD CONSTRAINT {table}_user_id_fkey" in sql, f"missing ADD CONSTRAINT for {table}"

    executable = "\n".join(ln for ln in sql.splitlines() if not ln.lstrip().startswith("--"))
    # One DO $$ BEGIN … EXCEPTION WHEN duplicate_object per table.
    assert executable.count("EXCEPTION WHEN duplicate_object THEN NULL;") == len(ALL_TABLES)


def test_migration_082_cascade_tables_use_on_delete_cascade() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    executable = "\n".join(ln for ln in sql.splitlines() if not ln.lstrip().startswith("--"))
    assert executable.count("REFERENCES users(id) ON DELETE CASCADE") == len(CASCADE_TABLES)


def test_migration_082_pulse_models_uses_on_delete_set_null() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    executable = "\n".join(ln for ln in sql.splitlines() if not ln.lstrip().startswith("--"))
    assert executable.count("REFERENCES users(id) ON DELETE SET NULL") == len(SET_NULL_TABLES)


def test_migration_082_self_heals_null_user_rows_no_raise() -> None:
    """RB-D: the pre-flight must self-heal NULL-user rows (per-table backup +
    reassign-to-first-admin), NOT RAISE/abort (which crash-looped the runner).
    """
    sql = MIGRATION.read_text(encoding="utf-8")
    # The crash-loop RAISE EXCEPTION must be gone.
    assert "RAISE EXCEPTION" not in sql, "082 must self-heal, not RAISE/abort"
    # Self-heal must reference all 6 CASCADE tables.
    array_block = sql.split("cascade_tables TEXT[] := ARRAY[")[1].split("]")[0]
    for table in CASCADE_TABLES:
        assert f"'{table}'" in array_block, f"table {table!r} missing from self-heal ARRAY"
    # pulse_models must NOT be in the self-heal set (SET NULL, nulls intentional).
    assert "'pulse_models'" not in array_block
    # Per-table idempotent backup before mutating (mirrors migrate_null_user_data.py).
    assert "CREATE TABLE IF NOT EXISTS" in sql
    assert "_null_user_backup_082" in sql
    # Reassign to the oldest active admin (NULL → 0-row no-op on fresh installs).
    # The admin sub-select lives inside a format() string literal, so single
    # quotes are doubled in the file text ('' == ' once rendered by Postgres).
    assert "role = ''admin''" in sql
    assert "deleted_at IS NULL" in sql
    assert "SET user_id =" in sql
    # No DELETE — reassign-only, matching the safe script's first_admin strategy.
    assert "DELETE FROM" not in sql.upper().replace("ON DELETE", "")
    # Pointer to the advanced-cases tool must remain in the comment block.
    assert "scripts/migrate_null_user_data.py" in sql


def test_migration_082_no_outer_transaction() -> None:
    """Runner wraps each migration; migration must not BEGIN/COMMIT itself."""
    sql = MIGRATION.read_text(encoding="utf-8")
    for line in sql.splitlines():
        stripped = line.strip().upper()
        assert not stripped.startswith(("BEGIN;", "COMMIT;", "ROLLBACK;"))


# ---------------------------------------------------------------------------
# Live-PG: opt-in via JARVIS_RUN_LIVE_PG=1 (Docker-backed fixture).
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[3]
_INIT_SQL = _REPO_ROOT / "db" / "init.sql"


async def _apply_fresh_init(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(_INIT_SQL.read_text(encoding="utf-8"))


@pytest.mark.live_pg
@pytest.mark.asyncio
async def test_migration_082_live_pg_fks_exist_and_no_orphans(live_pg_dsn: str) -> None:
    """Apply all migrations through 082 and verify:
    1. Each of the 7 FK constraints exists with the correct delete rule.
    2. Zero orphan rows remain in any of the 7 tables.
    """
    from paper_ingestion.migrations_runner import run_migrations

    pool = await asyncpg.create_pool(live_pg_dsn, min_size=1, max_size=2)
    try:
        await _apply_fresh_init(pool)
        await run_migrations(pool)

        async with pool.acquire() as conn:
            # 1. Verify each FK constraint exists with the correct delete rule.
            for table in CASCADE_TABLES:
                row = await conn.fetchrow(
                    """
                    SELECT rc.delete_rule, ccu.table_name AS referenced_table
                      FROM information_schema.table_constraints tc
                      JOIN information_schema.referential_constraints rc
                        ON tc.constraint_name = rc.constraint_name
                      JOIN information_schema.constraint_column_usage ccu
                        ON tc.constraint_name = ccu.constraint_name
                     WHERE tc.constraint_type = 'FOREIGN KEY'
                       AND tc.table_name = $1
                       AND tc.constraint_name = $2
                    """,
                    table,
                    f"{table}_user_id_fkey",
                )
                assert row is not None, f"FK {table}_user_id_fkey not found"
                assert row["delete_rule"] == "CASCADE", (
                    f"{table}: expected ON DELETE CASCADE, got {row['delete_rule']}"
                )
                assert row["referenced_table"] == "users"

            for table in SET_NULL_TABLES:
                row = await conn.fetchrow(
                    """
                    SELECT rc.delete_rule, ccu.table_name AS referenced_table
                      FROM information_schema.table_constraints tc
                      JOIN information_schema.referential_constraints rc
                        ON tc.constraint_name = rc.constraint_name
                      JOIN information_schema.constraint_column_usage ccu
                        ON tc.constraint_name = ccu.constraint_name
                     WHERE tc.constraint_type = 'FOREIGN KEY'
                       AND tc.table_name = $1
                       AND tc.constraint_name = $2
                    """,
                    table,
                    f"{table}_user_id_fkey",
                )
                assert row is not None, f"FK {table}_user_id_fkey not found"
                assert row["delete_rule"] == "SET NULL", (
                    f"{table}: expected ON DELETE SET NULL, got {row['delete_rule']}"
                )
                assert row["referenced_table"] == "users"

            # 2. Verify zero orphan rows (user_id NOT NULL and no matching users row).
            for table in ALL_TABLES:
                orphan_count = await conn.fetchval(
                    f"SELECT count(*) FROM {table} WHERE user_id IS NOT NULL"  # noqa: S608
                    " AND user_id NOT IN (SELECT id FROM users)"
                )
                assert orphan_count == 0, (
                    f"{table}: {orphan_count} orphan row(s) remain after migration 082"
                )
    finally:
        await pool.close()


async def _rewind_before_082(pool: asyncpg.Pool) -> None:
    """Reproduce a pre-082 schema state on a fully-migrated DB.

    Building a partial pre-082 chain by replaying early migrations on top of
    init.sql is not viable (init.sql already carries later schema, so the early
    ALTERs conflict). Instead we let the runner build the full schema, then
    surgically rewind only what 082/087 created: drop the 6 CASCADE FKs and
    un-record versions 82 & 87 from ``schema_migrations`` so the next
    ``run_migrations`` re-applies 082 (and 087) against whatever NULL-user data
    the test then injects — exactly the pre-v0.4.0 upgrade condition.
    """
    from paper_ingestion.migrations_runner import run_migrations

    await _apply_fresh_init(pool)
    await run_migrations(pool)
    async with pool.acquire() as conn:
        for table in CASCADE_TABLES:
            await conn.execute(
                f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {table}_user_id_fkey"  # noqa: S608
            )
        await conn.execute("DELETE FROM schema_migrations WHERE version IN (82, 87)")


@pytest.mark.live_pg
@pytest.mark.asyncio
async def test_migration_082_self_heals_null_user_rows_with_admin(live_pg_dsn: str) -> None:
    """RB-D: a pre-082 DB with a NULL-user CASCADE row + an admin migrates
    clean — the row is reassigned to the admin and a backup table is present,
    instead of the runner crash-looping on a RAISE.
    """
    from paper_ingestion.migrations_runner import run_migrations

    pool = await asyncpg.create_pool(live_pg_dsn, min_size=1, max_size=2)
    try:
        await _rewind_before_082(pool)
        async with pool.acquire() as conn:
            admin_id = await conn.fetchval(
                "INSERT INTO users (email, role) VALUES ('admin082@example.com', 'admin') "
                "RETURNING id"
            )
            # Pre-082 NULL-user row in a CASCADE table (would have RAISEd before).
            await conn.execute(
                "INSERT INTO source_health (user_id, source_type) VALUES (NULL, 'arxiv')"
            )

        # Must NOT raise — 082 self-heals instead of aborting.
        await run_migrations(pool)

        async with pool.acquire() as conn:
            healed = await conn.fetchval(
                "SELECT user_id FROM source_health WHERE source_type = 'arxiv'"
            )
            assert healed == admin_id, "NULL-user row not reassigned to first admin"
            null_remaining = await conn.fetchval(
                "SELECT count(*) FROM source_health WHERE user_id IS NULL"
            )
            assert null_remaining == 0
            backup_rows = await conn.fetchval(
                "SELECT count(*) FROM source_health_null_user_backup_082"
            )
            assert backup_rows == 1, "backup table missing the healed row"
            # Idempotent: re-running the raw 082 body must not error or re-heal.
            await conn.execute(MIGRATION.read_text(encoding="utf-8"))
            assert (
                await conn.fetchval("SELECT count(*) FROM source_health WHERE user_id IS NULL")
            ) == 0
    finally:
        await pool.close()


@pytest.mark.live_pg
@pytest.mark.asyncio
async def test_migration_082_empty_db_still_passes(live_pg_dsn: str) -> None:
    """Empty DB (no NULL-user rows, no admin): 082 is a clean no-op — no backup
    table is created and no FK is left dangling.
    """
    from paper_ingestion.migrations_runner import run_migrations

    pool = await asyncpg.create_pool(live_pg_dsn, min_size=1, max_size=2)
    try:
        await _apply_fresh_init(pool)
        await run_migrations(pool)  # must not raise on a NULL-row-free DB

        async with pool.acquire() as conn:
            for table in CASCADE_TABLES:
                exists = await conn.fetchval(
                    "SELECT to_regclass($1)", f"{table}_null_user_backup_082"
                )
                assert exists is None, f"{table}: backup table created despite no NULL-user rows"
    finally:
        await pool.close()
