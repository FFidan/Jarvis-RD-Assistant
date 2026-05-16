"""Structural and live-PG tests for migration 086 (offline review sync dedupe).

Migration 086 adds, idempotently:

- ``review_logs.idempotency_key`` (TEXT, nullable) — the client-minted
  per-review dedupe token.
- ``uq_review_logs_user_idempotency`` — a partial UNIQUE index on
  ``(user_id, idempotency_key)`` WHERE ``idempotency_key IS NOT NULL`` so
  historical NULL-key rows never collide.

Text-based assertions verify the SQL artifact; the opt-in live-PG test applies
all migrations through 086 against a disposable database and confirms the
column + unique index exist, the column is nullable, the partial unique
constraint actually dedupes per user, and that re-applying the migration is a
no-op (idempotent). Mirrors ``test_migration_085`` in the paper_ingestion suite.
"""

from __future__ import annotations

from pathlib import Path

import asyncpg
import pytest

MIGRATION = Path(__file__).resolve().parents[3] / "db/migrations/086_review_logs_idempotency.sql"


# ---------------------------------------------------------------------------
# Text-based correctness — always runs (no DB dependency).
# ---------------------------------------------------------------------------


def _executable(sql: str) -> str:
    return "\n".join(ln for ln in sql.splitlines() if not ln.lstrip().startswith("--"))


def test_migration_086_file_exists() -> None:
    assert MIGRATION.is_file(), f"Missing migration file: {MIGRATION}"


def test_migration_086_adds_idempotency_key_idempotently() -> None:
    sql = _executable(MIGRATION.read_text(encoding="utf-8"))
    assert "ALTER TABLE review_logs" in sql
    assert "ADD COLUMN IF NOT EXISTS idempotency_key TEXT" in sql


def test_migration_086_creates_partial_unique_index() -> None:
    sql = _executable(MIGRATION.read_text(encoding="utf-8"))
    assert "CREATE UNIQUE INDEX IF NOT EXISTS uq_review_logs_user_idempotency" in sql
    assert "(user_id, idempotency_key)" in sql
    assert "WHERE idempotency_key IS NOT NULL" in sql


def test_migration_086_column_is_nullable() -> None:
    """Additive column: the ALTER carries no NOT NULL / DEFAULT (no backfill).

    Asserted only against the ALTER TABLE statement — the partial index's
    ``WHERE idempotency_key IS NOT NULL`` predicate legitimately contains the
    substring "NOT NULL" and must not trip this guard.
    """
    executable = _executable(MIGRATION.read_text(encoding="utf-8"))
    alter_line = next(
        ln for ln in executable.splitlines() if ln.strip().upper().startswith("ALTER TABLE")
    ).upper()
    assert "NOT NULL" not in alter_line
    assert "DEFAULT" not in alter_line


def test_migration_086_no_outer_transaction() -> None:
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
async def test_migration_086_live_pg_column_index_and_dedupe(live_pg_dsn: str) -> None:
    """Apply all migrations through 086 and verify:

    1. ``review_logs.idempotency_key`` exists, is TEXT, nullable.
    2. ``uq_review_logs_user_idempotency`` is a UNIQUE index.
    3. The partial unique constraint dedupes per (user_id, idempotency_key)
       while allowing many NULL-key rows.
    4. Re-running migration 086 is a no-op (IF NOT EXISTS).
    """
    from paper_ingestion.migrations_runner import run_migrations

    pool = await asyncpg.create_pool(live_pg_dsn, min_size=1, max_size=2)
    try:
        await _apply_fresh_init(pool)
        await run_migrations(pool)

        async with pool.acquire() as conn:
            col = await conn.fetchrow(
                """
                SELECT data_type, is_nullable
                  FROM information_schema.columns
                 WHERE table_name = 'review_logs'
                   AND column_name = 'idempotency_key'
                """
            )
            assert col is not None, "review_logs.idempotency_key missing after 086"
            assert col["data_type"] == "text"
            assert col["is_nullable"] == "YES"

            idx = await conn.fetchrow(
                """
                SELECT indexdef
                  FROM pg_indexes
                 WHERE tablename = 'review_logs'
                   AND indexname = 'uq_review_logs_user_idempotency'
                """
            )
            assert idx is not None, "unique index missing after 086"
            assert "UNIQUE" in idx["indexdef"]

            # Seed a user + card so review_logs FKs resolve.
            user_id = await conn.fetchval(
                "INSERT INTO users (email) VALUES ('m086@example.com') RETURNING id"
            )
            card_id = await conn.fetchval(
                """
                INSERT INTO cards (card_type, front, back, user_id)
                VALUES ('concept', 'Q', 'A', $1) RETURNING id
                """,
                user_id,
            )

            async def _insert(key: str | None) -> None:
                await conn.execute(
                    """
                    INSERT INTO review_logs
                        (card_id, rating, fsrs_log, user_id, idempotency_key)
                    VALUES ($1, 3, '{}'::jsonb, $2, $3)
                    """,
                    card_id,
                    user_id,
                    key,
                )

            await _insert("k1")
            with pytest.raises(asyncpg.UniqueViolationError):
                await _insert("k1")  # same (user_id, key) → blocked

            # NULL keys are exempt from the partial index — many allowed.
            await _insert(None)
            await _insert(None)

            # Idempotency: re-applying the raw migration body must not error.
            await conn.execute(MIGRATION.read_text(encoding="utf-8"))
    finally:
        await pool.close()
