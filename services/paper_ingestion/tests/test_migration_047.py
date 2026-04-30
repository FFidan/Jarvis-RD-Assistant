"""Structural and live-PG tests for migration 047 (paper_user_state collapse to state ENUM)."""

from __future__ import annotations

from pathlib import Path

import asyncpg
import pytest

MIGRATION = Path(__file__).resolve().parents[3] / "db/migrations/047_paper_user_state_collapse.sql"

# ---------------------------------------------------------------------------
# Text-match smoke tests (always run — no Docker required)
# ---------------------------------------------------------------------------


def test_migration_047_text() -> None:
    """Migration 047 SQL must contain all expected DDL/DML snippets."""
    sql = MIGRATION.read_text(encoding="utf-8")

    # New columns present with correct CHECK constraints
    assert "ADD COLUMN IF NOT EXISTS state TEXT NOT NULL DEFAULT 'inbox'" in sql
    assert "CHECK (state IN ('inbox', 'to_read', 'reading', 'done', 'trash'))" in sql
    assert "ADD COLUMN IF NOT EXISTS state_before_trash TEXT" in sql
    assert "state_before_trash IN ('inbox', 'to_read', 'reading', 'done')" in sql

    # Backfill order is significant — assert dismissed wins, then archived/read, then reading, then saved.
    assert "WHEN dismissed = TRUE                       THEN 'trash'" in sql
    assert "WHEN archived = TRUE OR status = 'read'     THEN 'done'" in sql
    assert "WHEN status = 'reading'                     THEN 'reading'" in sql
    assert "WHEN saved = TRUE                           THEN 'to_read'" in sql

    # state_before_trash backfill for trash rows (excludes the dismissed branch).
    assert "WHERE state = 'trash'" in sql

    # Legacy columns must be dropped.
    assert "DROP COLUMN IF EXISTS saved" in sql
    assert "DROP COLUMN IF EXISTS dismissed" in sql
    assert "DROP COLUMN IF EXISTS archived" in sql
    assert "DROP COLUMN IF EXISTS status" in sql
    assert "DROP COLUMN IF EXISTS preference" in sql

    # State index for view-predicate query performance.
    assert "CREATE INDEX IF NOT EXISTS idx_paper_user_state_state ON paper_user_state(state)" in sql


# ---------------------------------------------------------------------------
# Live-PG tests (gated by JARVIS_RUN_LIVE_PG=1, marked live_pg)
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[3]
_INIT_SQL = _REPO_ROOT / "db" / "init.sql"


async def _apply_fresh_init(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(_INIT_SQL.read_text(encoding="utf-8"))


@pytest.mark.live_pg
@pytest.mark.asyncio
async def test_migration_047_live_pg(live_pg_dsn: str) -> None:
    """Apply migrations through 047 and verify state column shape + Restore semantics."""
    from paper_ingestion.migrations_runner import run_migrations

    pool = await asyncpg.create_pool(live_pg_dsn, min_size=1, max_size=2)
    try:
        await _apply_fresh_init(pool)
        await run_migrations(pool)

        async with pool.acquire() as conn:
            # Legacy columns must be gone post-migration.
            cols = await conn.fetch(
                """
                SELECT column_name FROM information_schema.columns
                 WHERE table_name = 'paper_user_state'
                """
            )
            col_names = {r["column_name"] for r in cols}
            assert "state" in col_names
            assert "state_before_trash" in col_names
            assert "starred" in col_names
            assert "saved" not in col_names, "Legacy column 'saved' must be dropped"
            assert "dismissed" not in col_names, "Legacy column 'dismissed' must be dropped"
            assert "archived" not in col_names, "Legacy column 'archived' must be dropped"
            assert "status" not in col_names, "Legacy column 'status' must be dropped"
            assert "preference" not in col_names, "Legacy column 'preference' must be dropped"

            # Insert a paper to anchor user_state rows.
            await conn.execute(
                """
                INSERT INTO papers (external_id, source_type, title, authors, url)
                VALUES ('live-pg-047', 'arxiv', 'Live PG 047', ARRAY['Tester'],
                        'https://example.test/047')
                """
            )
            paper_id = await conn.fetchval(
                "SELECT id FROM papers WHERE external_id = $1",
                "live-pg-047",
            )

            # Valid state insert succeeds.
            await conn.execute(
                """
                INSERT INTO paper_user_state (paper_id, user_id, state)
                VALUES ($1, 1, 'to_read')
                """,
                paper_id,
            )
            state = await conn.fetchval(
                "SELECT state FROM paper_user_state WHERE paper_id = $1 AND user_id = 1",
                paper_id,
            )
            assert state == "to_read"

            # Invalid state must be rejected.
            with pytest.raises(asyncpg.exceptions.CheckViolationError):
                await conn.execute(
                    """
                    INSERT INTO paper_user_state (paper_id, user_id, state)
                    VALUES ($1, 99, 'archived')
                    """,
                    paper_id,
                )

            # Restore semantics: trash with state_before_trash → restore returns to that state.
            await conn.execute(
                """
                UPDATE paper_user_state
                   SET state_before_trash = state, state = 'trash'
                 WHERE paper_id = $1 AND user_id = 1
                """,
                paper_id,
            )
            state, before = await conn.fetchrow(
                """
                SELECT state, state_before_trash
                  FROM paper_user_state
                 WHERE paper_id = $1 AND user_id = 1
                """,
                paper_id,
            )
            assert state == "trash"
            assert before == "to_read"

            # Restore: state ← state_before_trash; state_before_trash ← NULL.
            await conn.execute(
                """
                UPDATE paper_user_state
                   SET state = state_before_trash, state_before_trash = NULL
                 WHERE paper_id = $1 AND user_id = 1
                """,
                paper_id,
            )
            state, before = await conn.fetchrow(
                """
                SELECT state, state_before_trash
                  FROM paper_user_state
                 WHERE paper_id = $1 AND user_id = 1
                """,
                paper_id,
            )
            assert state == "to_read"
            assert before is None

    finally:
        await pool.close()
