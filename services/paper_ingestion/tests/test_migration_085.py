"""Structural and live-PG tests for migration 085 (UI_v3 §I Account).

Migration 085 adds two nullable columns, both idempotently
(``ADD COLUMN IF NOT EXISTS``):

- ``users.display_name`` (TEXT) — the self-service profile display name.
- ``magic_link_tokens.pending_email`` (TEXT) — overloads the existing
  single-use magic-link token table for the verified email-change flow.

Text-based assertions verify the SQL artifact; the opt-in live-PG test
applies all migrations through 085 against a disposable database and
confirms both columns exist, are nullable, and that re-applying the
migration is a no-op (idempotent).
"""

from __future__ import annotations

from pathlib import Path

import asyncpg
import pytest

MIGRATION = Path(__file__).resolve().parents[3] / "db/migrations/085_users_display_name.sql"


# ---------------------------------------------------------------------------
# Text-based correctness — always runs (no DB dependency).
# ---------------------------------------------------------------------------


def test_migration_085_file_exists() -> None:
    assert MIGRATION.is_file(), f"Missing migration file: {MIGRATION}"


def test_migration_085_adds_display_name_idempotently() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    executable = "\n".join(ln for ln in sql.splitlines() if not ln.lstrip().startswith("--"))
    assert "ALTER TABLE users" in executable
    assert "ADD COLUMN IF NOT EXISTS display_name TEXT" in executable


def test_migration_085_adds_pending_email_idempotently() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    executable = "\n".join(ln for ln in sql.splitlines() if not ln.lstrip().startswith("--"))
    assert "ALTER TABLE magic_link_tokens" in executable
    assert "ADD COLUMN IF NOT EXISTS pending_email TEXT" in executable


def test_migration_085_columns_are_nullable() -> None:
    """Neither column may carry NOT NULL / DEFAULT — additive, no backfill."""
    sql = MIGRATION.read_text(encoding="utf-8")
    executable = "\n".join(ln for ln in sql.splitlines() if not ln.lstrip().startswith("--"))
    upper = executable.upper()
    assert "NOT NULL" not in upper
    assert "DEFAULT" not in upper


def test_migration_085_no_outer_transaction() -> None:
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


async def _column_is_nullable(conn: asyncpg.Connection, table: str, column: str) -> bool:
    row = await conn.fetchrow(
        """
        SELECT is_nullable, data_type
          FROM information_schema.columns
         WHERE table_name = $1 AND column_name = $2
        """,
        table,
        column,
    )
    assert row is not None, f"{table}.{column} does not exist after migration 085"
    assert row["data_type"] == "text", f"{table}.{column}: expected text, got {row['data_type']}"
    return row["is_nullable"] == "YES"


@pytest.mark.live_pg
@pytest.mark.asyncio
async def test_migration_085_live_pg_columns_exist_and_idempotent(live_pg_dsn: str) -> None:
    """Apply all migrations through 085 and verify:

    1. ``users.display_name`` exists, is TEXT, and is nullable.
    2. ``magic_link_tokens.pending_email`` exists, is TEXT, and is nullable.
    3. Re-running migration 085 is a no-op (ADD COLUMN IF NOT EXISTS).
    """
    from paper_ingestion.migrations_runner import run_migrations

    pool = await asyncpg.create_pool(live_pg_dsn, min_size=1, max_size=2)
    try:
        await _apply_fresh_init(pool)
        await run_migrations(pool)

        async with pool.acquire() as conn:
            assert await _column_is_nullable(conn, "users", "display_name")
            assert await _column_is_nullable(conn, "magic_link_tokens", "pending_email")

            # Idempotency: re-applying the raw migration body must not error.
            await conn.execute(MIGRATION.read_text(encoding="utf-8"))

            assert await _column_is_nullable(conn, "users", "display_name")
            assert await _column_is_nullable(conn, "magic_link_tokens", "pending_email")
    finally:
        await pool.close()
