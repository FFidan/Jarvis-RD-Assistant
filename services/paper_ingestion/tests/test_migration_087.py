"""Structural and live-PG tests for migration 087 (pulse_models user_id index).

Migration 087 (DB-F04) adds, idempotently:

- ``idx_pulse_models_user_id`` — a plain btree index on
  ``pulse_models(user_id)`` supporting per-user pulse-model lookups and the
  mig-082 ``pulse_models_user_id_fkey`` ON DELETE SET NULL cascade.

(DB-F03 / journal_entries was verified FALSE — its ``(user_id, date)``
composite index already covers user_id-prefixed lookups, so no index is added
there; that table is intentionally absent from this migration and its tests.)

Text-based assertions verify the SQL artifact; the opt-in live-PG test applies
all migrations through 087 against a disposable database and confirms the index
exists on the right column and that re-applying the migration is a no-op.
Mirrors ``test_migration_086`` in the learning_engine suite.
"""

from __future__ import annotations

from pathlib import Path

import asyncpg
import pytest

MIGRATION = Path(__file__).resolve().parents[3] / "db/migrations/087_pulse_models_user_id_index.sql"


# ---------------------------------------------------------------------------
# Text-based correctness — always runs (no DB dependency).
# ---------------------------------------------------------------------------


def _executable(sql: str) -> str:
    return "\n".join(ln for ln in sql.splitlines() if not ln.lstrip().startswith("--"))


def test_migration_087_file_exists() -> None:
    assert MIGRATION.is_file(), f"Missing migration file: {MIGRATION}"


def test_migration_087_creates_index_idempotently() -> None:
    sql = _executable(MIGRATION.read_text(encoding="utf-8"))
    assert "CREATE INDEX IF NOT EXISTS idx_pulse_models_user_id" in sql
    assert "ON pulse_models(user_id)" in sql


def test_migration_087_does_not_touch_journal_entries() -> None:
    """DB-F03 is verified FALSE — 087 must not add a journal_entries index."""
    sql = _executable(MIGRATION.read_text(encoding="utf-8"))
    assert "journal_entries" not in sql


def test_migration_087_no_outer_transaction() -> None:
    """Runner wraps each migration; migration must not BEGIN/COMMIT itself."""
    for line in MIGRATION.read_text(encoding="utf-8").splitlines():
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
async def test_migration_087_live_pg_index_exists(live_pg_dsn: str) -> None:
    """Apply all migrations through 087 and verify:

    1. ``idx_pulse_models_user_id`` exists on ``pulse_models``.
    2. The index is over the ``user_id`` column.
    3. Re-running migration 087 is a no-op (IF NOT EXISTS).
    """
    from paper_ingestion.migrations_runner import run_migrations

    pool = await asyncpg.create_pool(live_pg_dsn, min_size=1, max_size=2)
    try:
        await _apply_fresh_init(pool)
        await run_migrations(pool)

        async with pool.acquire() as conn:
            idx = await conn.fetchrow(
                """
                SELECT indexdef
                  FROM pg_indexes
                 WHERE tablename = 'pulse_models'
                   AND indexname = 'idx_pulse_models_user_id'
                """
            )
            assert idx is not None, "idx_pulse_models_user_id missing after 087"
            assert "user_id" in idx["indexdef"]

            # Idempotent: re-applying the raw migration body must not error.
            await conn.execute(MIGRATION.read_text(encoding="utf-8"))
    finally:
        await pool.close()
