"""Structural and live-PG tests for migration 084 (project_questions).

Migration 084 adds the ``project_questions`` table (Projects IA redesign §4a):
per-project open research questions, FK to projects (ON DELETE CASCADE) and
users (ON DELETE CASCADE), scoped by user_id. Text-based assertions verify the
SQL artifact is idempotent and transaction-free; the live-PG test applies all
migrations and confirms the table, columns, and FK delete rules.
"""

from __future__ import annotations

from pathlib import Path

import asyncpg
import pytest

MIGRATION = Path(__file__).resolve().parents[3] / "db/migrations/084_project_questions.sql"


# ---------------------------------------------------------------------------
# Text-based correctness — always runs (no DB dependency).
# ---------------------------------------------------------------------------


def test_migration_084_is_idempotent() -> None:
    """Table + indexes must use IF NOT EXISTS so a re-apply is harmless."""
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS project_questions" in sql
    assert "CREATE INDEX IF NOT EXISTS idx_project_questions_project" in sql
    assert "CREATE INDEX IF NOT EXISTS idx_project_questions_user" in sql


def test_migration_084_fks_cascade() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    executable = "\n".join(ln for ln in sql.splitlines() if not ln.lstrip().startswith("--"))
    assert "REFERENCES projects(id) ON DELETE CASCADE" in executable
    assert "REFERENCES users(id)    ON DELETE CASCADE" in executable or (
        "REFERENCES users(id) ON DELETE CASCADE" in executable
    )


def test_migration_084_required_columns() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    for col in ("id", "project_id", "user_id", "body", "created_at"):
        assert col in sql, f"missing column {col!r} in migration 084"
    assert "body        TEXT NOT NULL" in sql or "body TEXT NOT NULL" in sql


def test_migration_084_no_outer_transaction() -> None:
    """Runner wraps each migration; migration must not BEGIN/COMMIT itself."""
    sql = MIGRATION.read_text(encoding="utf-8")
    for line in sql.splitlines():
        stripped = line.strip().upper()
        assert not stripped.startswith(("BEGIN;", "COMMIT;", "ROLLBACK;"))


# ---------------------------------------------------------------------------
# Live-PG: opt-in via JARVIS_RUN_LIVE_PG=1 (Docker-backed fixture).
# ---------------------------------------------------------------------------

from tests.migration_helpers import apply_fresh_init as _apply_fresh_init


@pytest.mark.live_pg
@pytest.mark.asyncio
async def test_migration_084_live_pg_table_and_fks(live_pg_dsn: str) -> None:
    """Apply all migrations through 084 and verify the table + FK delete rules."""
    from paper_ingestion.migrations_runner import run_migrations

    pool = await asyncpg.create_pool(live_pg_dsn, min_size=1, max_size=2)
    try:
        await _apply_fresh_init(pool)
        await run_migrations(pool)

        async with pool.acquire() as conn:
            # Table exists with the expected columns.
            cols = {
                r["column_name"]: r["is_nullable"]
                for r in await conn.fetch(
                    """
                    SELECT column_name, is_nullable
                      FROM information_schema.columns
                     WHERE table_name = 'project_questions'
                    """
                )
            }
            assert {"id", "project_id", "user_id", "body", "created_at"} <= set(cols)
            assert cols["body"] == "NO"
            assert cols["project_id"] == "NO"
            assert cols["user_id"] == "NO"

            # Both FKs cascade on parent delete.
            fk_rules = {
                r["referenced_table"]: r["delete_rule"]
                for r in await conn.fetch(
                    """
                    SELECT ccu.table_name AS referenced_table, rc.delete_rule
                      FROM information_schema.table_constraints tc
                      JOIN information_schema.referential_constraints rc
                        ON tc.constraint_name = rc.constraint_name
                      JOIN information_schema.constraint_column_usage ccu
                        ON tc.constraint_name = ccu.constraint_name
                     WHERE tc.constraint_type = 'FOREIGN KEY'
                       AND tc.table_name = 'project_questions'
                    """
                )
            }
            assert fk_rules.get("projects") == "CASCADE"
            assert fk_rules.get("users") == "CASCADE"
    finally:
        await pool.close()
