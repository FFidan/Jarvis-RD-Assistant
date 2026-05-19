"""Structural and live-PG tests for migration 077 (user_id FK constraints).

Migration 077 adds `<table>_user_id_fkey` constraints on 18 tables whose
`user_id` columns were added by earlier migrations (042/062-066/070) without
REFERENCES clauses. Text-based assertions verify the SQL artifact; the
live-PG test exercises a fresh apply and orphan handling.
"""

from __future__ import annotations

from pathlib import Path

import asyncpg
import pytest

MIGRATION = Path(__file__).resolve().parents[3] / "db/migrations/077_user_id_fk_constraints.sql"

# The 18 tables whose user_id columns need an FK on users(id).
EXPECTED_TABLES: tuple[str, ...] = (
    "papers",
    "paper_notes",
    "paper_summaries",
    "paper_chunks",
    "paper_user_state",
    "pulse_cards",
    "paper_contradictions",
    "paper_extractions",
    "daily_log",
    "paper_recommendations",
    "projects",
    "tasks",
    "milestones",
    "cards",
    "decks",
    "review_logs",
    "tracked_authors",
    "author_alert_log",
)


# ---------------------------------------------------------------------------
# Text-based correctness — always runs (no DB dependency).
# ---------------------------------------------------------------------------


def test_migration_077_covers_all_18_tables() -> None:
    """Every expected table must appear with: orphan NULL-out and ADD CONSTRAINT
    <table>_user_id_fkey REFERENCES users, wrapped in the canonical idempotent
    DO $$ … EXCEPTION WHEN duplicate_object guard from migration 051.
    """
    sql = MIGRATION.read_text(encoding="utf-8")

    for table in EXPECTED_TABLES:
        # `papers.user_id` was renamed to `papers.discovered_by` in mig 072
        # (canonical-corpus model), so its FK lives on `discovered_by`.
        col = "discovered_by" if table == "papers" else "user_id"

        # 1. Orphan NULL-out for this specific table.
        assert f"UPDATE {table}\n   SET {col} = NULL" in sql, f"missing orphan NULL-out for {table}"

        # 2. The ADD CONSTRAINT statement itself, on the expected table.
        assert f"ALTER TABLE {table}" in sql, f"missing ALTER TABLE for {table}"
        assert f"ADD CONSTRAINT {table}_{col}_fkey" in sql, f"missing ADD CONSTRAINT for {table}"

    # Canonical idempotent guard: one DO $$ BEGIN … EXCEPTION WHEN
    # duplicate_object per table (matches scripts/check-migrations-no-tx.sh
    # Check 4 and the migration 051 pattern).
    executable_sql = "\n".join(
        line for line in sql.splitlines() if not line.lstrip().startswith("--")
    )
    assert executable_sql.count("DO $$ BEGIN") == len(EXPECTED_TABLES)
    assert executable_sql.count("EXCEPTION WHEN duplicate_object THEN NULL;") == len(
        EXPECTED_TABLES
    )

    # All FKs use ON DELETE SET NULL (matches migs 072-076 convention; CASCADE
    # would be destructive across user-shared tables).
    assert executable_sql.count("ON DELETE SET NULL") == len(EXPECTED_TABLES)
    assert "ON DELETE CASCADE" not in executable_sql
    # Defensive: no ON UPDATE clauses (none of migs 072-076 use them).
    assert "ON UPDATE" not in executable_sql


def test_migration_077_references_users_table() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    executable_sql = "\n".join(
        line for line in sql.splitlines() if not line.lstrip().startswith("--")
    )
    # Every ADD CONSTRAINT must reference users(id).
    assert executable_sql.count("REFERENCES users(id) ON DELETE SET NULL") == len(EXPECTED_TABLES)


# ---------------------------------------------------------------------------
# Live-PG: opt-in via JARVIS_RUN_LIVE_PG=1 (Docker-backed fixture).
# ---------------------------------------------------------------------------


from tests.migration_helpers import apply_fresh_init as _apply_fresh_init


@pytest.mark.live_pg
@pytest.mark.asyncio
async def test_migration_077_live_pg_adds_all_fks(live_pg_dsn: str) -> None:
    """Apply migrations through 077 and verify each table has the expected
    FK constraint with ON DELETE SET NULL referencing users(id).
    """
    from paper_ingestion.migrations_runner import run_migrations

    pool = await asyncpg.create_pool(live_pg_dsn, min_size=1, max_size=2)
    try:
        await _apply_fresh_init(pool)
        await run_migrations(pool)

        async with pool.acquire() as conn:
            for table in EXPECTED_TABLES:
                row = await conn.fetchrow(
                    """
                    SELECT rc.delete_rule, ccu.table_name AS referenced_table,
                           ccu.column_name AS referenced_column
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
                assert row["referenced_column"] == "id"
    finally:
        await pool.close()


@pytest.mark.live_pg
@pytest.mark.asyncio
async def test_migration_077_live_pg_nulls_orphans(live_pg_dsn: str) -> None:
    """Pre-existing orphan user_ids must be NULLed before the FK is added.

    Strategy: apply migrations up through 076, insert a row in `papers` with
    a non-existent user_id, then apply 077 manually and verify the orphan
    became NULL. (Using `papers` as a representative table — the same UPDATE
    pattern is repeated for all 18 tables.)
    """
    from jarvis_common import migrations as _migrations

    pool = await asyncpg.create_pool(live_pg_dsn, min_size=1, max_size=2)
    try:
        await _apply_fresh_init(pool)

        # Apply only migrations < 077 by temporarily skipping 077.
        # Simplest path: run the full runner (077 will no-op on a fresh DB
        # because there are no orphans), then manually create an orphan,
        # then re-run the 077 SQL by hand (idempotent IF NOT EXISTS makes
        # the FK add a no-op the second time).
        await _migrations.run_migrations(pool)

        async with pool.acquire() as conn:
            # The FK is now in place. To exercise the orphan path, we must
            # temporarily drop the FK, insert an orphan, then re-apply 077.
            await conn.execute("ALTER TABLE papers DROP CONSTRAINT IF EXISTS papers_user_id_fkey")
            # Insert a paper with a bogus user_id (no users exist yet).
            paper_id = await conn.fetchval(
                """
                INSERT INTO papers (title, user_id)
                VALUES ('orphan-test', 999999)
                RETURNING id
                """
            )
            assert paper_id is not None

            # Re-apply migration 077 (idempotent).
            await conn.execute(MIGRATION.read_text(encoding="utf-8"))

            # The orphan user_id should now be NULL.
            user_id_after = await conn.fetchval(
                "SELECT user_id FROM papers WHERE id = $1", paper_id
            )
            assert user_id_after is None, (
                f"orphan user_id should be NULL after migration 077, got {user_id_after}"
            )

            # And the FK should be back in place.
            fk_exists = await conn.fetchval(
                """
                SELECT 1 FROM information_schema.table_constraints
                 WHERE table_name = 'papers'
                   AND constraint_name = 'papers_user_id_fkey'
                """
            )
            assert fk_exists == 1
    finally:
        await pool.close()
