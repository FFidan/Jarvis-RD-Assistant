"""Structural and live-PG tests for migration 083 (greenfield `thread` entity).

Migration 083 creates the ``thread`` table (UI_v3 My-Day § Open threads /
3-mode hero) with a user-scoped ``thread_user_id_fkey`` FK (ON DELETE CASCADE)
and an idempotent index. Text-based assertions verify the SQL artifact; the
live-PG test exercises a fresh apply and confirms the table, FK delete rule,
constraints and idempotency (re-applying is a no-op).
"""

from __future__ import annotations

from pathlib import Path

import asyncpg
import pytest

MIGRATION = Path(__file__).resolve().parents[3] / "db/migrations/083_threads.sql"


# ---------------------------------------------------------------------------
# Text-based correctness — always runs (no DB dependency).
# ---------------------------------------------------------------------------


def test_migration_083_file_exists() -> None:
    assert MIGRATION.is_file(), f"Missing migration file: {MIGRATION}"


def test_migration_083_creates_thread_table_idempotently() -> None:
    """Must CREATE TABLE IF NOT EXISTS thread with the spec §4.1 shape."""
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS thread" in sql
    # Spec §4.1 minimum shape.
    for col in ("id", "user_id", "title", "anchor", "progress", "last_at", "status", "created_at"):
        assert col in sql, f"missing column {col!r} in thread table"
    assert "REAL" in sql, "progress must be a REAL column"
    assert "INTEGER" in sql, "user_id must follow the INTEGER NULL convention"


def test_migration_083_fk_is_idempotent_and_cascade() -> None:
    """FK must be wrapped in the canonical idempotent DO $$ … guard and CASCADE."""
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "ADD CONSTRAINT thread_user_id_fkey" in sql
    assert "REFERENCES users(id) ON DELETE CASCADE" in sql
    executable = "\n".join(ln for ln in sql.splitlines() if not ln.lstrip().startswith("--"))
    assert executable.count("EXCEPTION WHEN duplicate_object THEN NULL;") == 1


def test_migration_083_creates_idempotent_index() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "CREATE INDEX IF NOT EXISTS idx_thread_user" in sql


def test_migration_083_no_outer_transaction() -> None:
    """Runner wraps each migration; migration must not BEGIN/COMMIT itself."""
    sql = MIGRATION.read_text(encoding="utf-8")
    for line in sql.splitlines():
        stripped = line.strip().upper()
        assert not stripped.startswith(("BEGIN;", "COMMIT;", "ROLLBACK;"))


# ---------------------------------------------------------------------------
# Live-PG: opt-in via the live_pg_dsn fixture (Docker-backed).
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[3]
_INIT_SQL = _REPO_ROOT / "db" / "init.sql"


async def _apply_fresh_init(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(_INIT_SQL.read_text(encoding="utf-8"))


@pytest.mark.live_pg
@pytest.mark.asyncio
async def test_migration_083_live_pg_table_fk_and_idempotent(live_pg_dsn: str) -> None:
    """Apply all migrations through 083 and verify:
    1. The ``thread`` table exists with the FK ON DELETE CASCADE → users.
    2. Re-applying migration 083 is a no-op (idempotent).
    3. The progress CHECK constraint rejects out-of-range values.
    4. ON DELETE CASCADE removes a user's threads.
    """
    from paper_ingestion.migrations_runner import run_migrations

    pool = await asyncpg.create_pool(live_pg_dsn, min_size=1, max_size=2)
    try:
        await _apply_fresh_init(pool)
        await run_migrations(pool)

        async with pool.acquire() as conn:
            # 1. Table + FK delete rule.
            row = await conn.fetchrow(
                """
                SELECT rc.delete_rule, ccu.table_name AS referenced_table
                  FROM information_schema.table_constraints tc
                  JOIN information_schema.referential_constraints rc
                    ON tc.constraint_name = rc.constraint_name
                  JOIN information_schema.constraint_column_usage ccu
                    ON tc.constraint_name = ccu.constraint_name
                 WHERE tc.constraint_type = 'FOREIGN KEY'
                   AND tc.table_name = 'thread'
                   AND tc.constraint_name = 'thread_user_id_fkey'
                """
            )
            assert row is not None, "FK thread_user_id_fkey not found"
            assert row["delete_rule"] == "CASCADE"
            assert row["referenced_table"] == "users"

            # 2. Idempotent re-apply.
            await conn.execute(MIGRATION.read_text(encoding="utf-8"))

            # 3. CHECK on progress.
            uid = await conn.fetchval(
                "INSERT INTO users (email, role) "
                "VALUES ('thread083@test.local', 'user') RETURNING id"
            )
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(
                    "INSERT INTO thread (user_id, title, progress) VALUES ($1, 't', 2.0)",
                    uid,
                )

            # 4. ON DELETE CASCADE.
            await conn.execute(
                "INSERT INTO thread (user_id, title, progress) VALUES ($1, 'kept', 0.5)",
                uid,
            )
            assert await conn.fetchval("SELECT count(*) FROM thread WHERE user_id = $1", uid) == 1
            await conn.execute("DELETE FROM users WHERE id = $1", uid)
            assert await conn.fetchval("SELECT count(*) FROM thread WHERE user_id = $1", uid) == 0
    finally:
        await pool.close()
