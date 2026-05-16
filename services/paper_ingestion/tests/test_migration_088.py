"""Structural and live-PG tests for migration 088 (B7 /my-day perf indexes).

Migration 088 adds, idempotently:

- ``idx_paper_recommendations_user_score_active`` — a composite partial btree
  index on ``paper_recommendations (user_id, score DESC) WHERE NOT dismissed``,
  serving the ``/api/executive/my-day`` recommendations query
  (``WHERE user_id = ? AND NOT dismissed ORDER BY score DESC``).

The two other indexes B7 originally proposed were verified redundant and are
intentionally NOT created:

- ``user_library (user_id, paper_id)`` is fully covered by
  ``user_library``'s ``PRIMARY KEY (user_id, paper_id)`` (mig 072).
- ``paper_topics (paper_id)`` is covered by ``paper_topics``'s
  ``PRIMARY KEY (paper_id, topic_id)`` (leading column is ``paper_id``).

Text-based assertions verify the SQL artifact; the opt-in live-PG test applies
all migrations through 088 against a disposable database and confirms the index
exists on the right table/predicate and that re-applying the migration is a
no-op. Mirrors ``test_migration_087``.
"""

from __future__ import annotations

from pathlib import Path

import asyncpg
import pytest

MIGRATION = Path(__file__).resolve().parents[3] / "db/migrations/088_perf_indexes.sql"


# ---------------------------------------------------------------------------
# Text-based correctness — always runs (no DB dependency).
# ---------------------------------------------------------------------------


def _executable(sql: str) -> str:
    return "\n".join(ln for ln in sql.splitlines() if not ln.lstrip().startswith("--"))


def test_migration_088_file_exists() -> None:
    assert MIGRATION.is_file(), f"Missing migration file: {MIGRATION}"


def test_migration_088_creates_partial_composite_index_idempotently() -> None:
    sql = _executable(MIGRATION.read_text(encoding="utf-8"))
    assert "CREATE INDEX IF NOT EXISTS idx_paper_recommendations_user_score_active" in sql
    assert "ON paper_recommendations (user_id, score DESC)" in sql
    assert "WHERE NOT dismissed" in sql


def test_migration_088_omits_redundant_indexes() -> None:
    """user_library / paper_topics indexes are redundant with their PKs — must be absent."""
    sql = _executable(MIGRATION.read_text(encoding="utf-8"))
    assert "ON user_library" not in sql
    assert "ON paper_topics" not in sql


def test_migration_088_no_outer_transaction() -> None:
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
async def test_migration_088_live_pg_index_exists(live_pg_dsn: str) -> None:
    """Apply all migrations through 088 and verify:

    1. ``idx_paper_recommendations_user_score_active`` exists on
       ``paper_recommendations``.
    2. The index is partial (``WHERE NOT dismissed``) over ``user_id, score``.
    3. The redundant user_library / paper_topics indexes were NOT created.
    4. Re-running migration 088 is a no-op (IF NOT EXISTS).
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
                 WHERE tablename = 'paper_recommendations'
                   AND indexname = 'idx_paper_recommendations_user_score_active'
                """
            )
            assert idx is not None, "idx_paper_recommendations_user_score_active missing after 088"
            assert "user_id" in idx["indexdef"]
            assert "score" in idx["indexdef"]
            assert "dismissed" in idx["indexdef"], "index must be partial on NOT dismissed"

            # Redundant indexes from the original B7 proposal must NOT exist.
            redundant = await conn.fetch(
                """
                SELECT indexname
                  FROM pg_indexes
                 WHERE indexname IN (
                     'idx_user_library_user_paper',
                     'idx_paper_topics_paper'
                 )
                """
            )
            assert redundant == [], (
                f"redundant indexes were created: {[r['indexname'] for r in redundant]}"
            )

            # Idempotent: re-applying the raw migration body must not error.
            await conn.execute(MIGRATION.read_text(encoding="utf-8"))
    finally:
        await pool.close()
