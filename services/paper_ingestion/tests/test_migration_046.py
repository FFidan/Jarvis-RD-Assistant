"""Structural and live-PG tests for migration 046 paper lifecycle triage."""

from __future__ import annotations

from pathlib import Path

import asyncpg
import pytest

MIGRATION = Path(__file__).resolve().parents[3] / "db/migrations/046_paper_lifecycle_triage.sql"

# ---------------------------------------------------------------------------
# Text-match smoke tests (always run — no Docker required)
# ---------------------------------------------------------------------------


def test_migration_046_text() -> None:
    """Migration 046 SQL must contain all expected DDL/DML snippets."""
    sql = MIGRATION.read_text(encoding="utf-8")

    assert "ADD COLUMN IF NOT EXISTS saved BOOLEAN NOT NULL DEFAULT FALSE" in sql
    assert "ADD COLUMN IF NOT EXISTS dismissed BOOLEAN NOT NULL DEFAULT FALSE" in sql
    assert "ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()" in sql
    assert "UPDATE paper_user_state SET saved = TRUE" in sql
    assert "WHERE status = 'starred'" in sql
    assert "WHERE status = 'archived'" in sql
    assert "CHECK (status IN ('new', 'reading', 'read'))" in sql
    assert "CREATE TRIGGER set_updated_at_paper_user_state" in sql


# ---------------------------------------------------------------------------
# Live-PG tests (gated by JARVIS_RUN_LIVE_PG=1, marked live_pg)
# ---------------------------------------------------------------------------

# Minimal pre-046 schema. Migration 046 operates on the paper_user_state shape
# that existed *before* migration 047 collapsed it into the `state` ENUM, so
# init.sql (the post-047 steady state) can never represent the pre-state this
# migration mutates. Build only what 046 touches: the `status` reading enum plus
# the starred/archived flags (added by migration 044) and the set_updated_at
# helper its trigger reuses.
_PRE_046_SCHEMA = """
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS TRIGGER AS $$
BEGIN NEW.updated_at = NOW(); RETURN NEW; END $$ LANGUAGE plpgsql;

CREATE TABLE papers (
    id          SERIAL PRIMARY KEY,
    external_id VARCHAR(255) UNIQUE NOT NULL,
    source_type VARCHAR(50) NOT NULL,
    title       TEXT NOT NULL,
    authors     TEXT[] NOT NULL,
    url         TEXT NOT NULL
);

CREATE TABLE paper_user_state (
    id        SERIAL PRIMARY KEY,
    paper_id  INTEGER REFERENCES papers(id) ON DELETE CASCADE,
    user_id   INTEGER,
    status    TEXT NOT NULL DEFAULT 'new',
    starred   BOOLEAN NOT NULL DEFAULT FALSE,
    archived  BOOLEAN NOT NULL DEFAULT FALSE
);
"""


async def _apply_pre_046(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(_PRE_046_SCHEMA)


@pytest.mark.live_pg
@pytest.mark.asyncio
async def test_migration_046_live_pg(live_pg_dsn: str) -> None:
    """Apply migration 046 onto a pre-046 schema and verify backfill, constraints, and idempotency."""
    pool = await asyncpg.create_pool(live_pg_dsn, min_size=1, max_size=2)
    try:
        await _apply_pre_046(pool)

        # Seed rows in the PRE-046 schema (no saved column yet) BEFORE migration.
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO papers (external_id, source_type, title, authors, url)
                VALUES ('live-pg-046', 'arxiv', 'Live PG 046', ARRAY['Tester'],
                        'https://example.test/046')
                """
            )
            paper_id = await conn.fetchval(
                "SELECT id FROM papers WHERE external_id = $1",
                "live-pg-046",
            )
            # Pre-migration rows: two with starred=TRUE, one plain reading row.
            # Migration backfill: UPDATE paper_user_state SET saved = TRUE WHERE starred = TRUE.
            await conn.execute(
                """
                INSERT INTO paper_user_state (paper_id, user_id, status, starred, archived)
                VALUES
                    ($1, 1, 'read',    TRUE,  FALSE),
                    ($1, 2, 'read',    FALSE, TRUE),
                    ($1, 3, 'reading', FALSE, FALSE)
                """,
                paper_id,
            )

        # Apply migration — must add saved column AND backfill starred/archived rows.
        await pool.execute(MIGRATION.read_text(encoding="utf-8"))

        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT user_id, status, starred, archived, saved
                FROM paper_user_state
                WHERE paper_id = $1
                ORDER BY user_id
                """,
                paper_id,
            )

            # All three rows must have saved=TRUE (migration backfill).
            assert all(r["saved"] is True for r in rows), (
                "Migration backfill must set saved=TRUE for all pre-existing rows"
            )

            # Row 1: status='read', starred=TRUE — starred row correctly saved.
            assert rows[0]["starred"] is True
            assert rows[0]["status"] == "read"

            # Row 2: status='read', archived=TRUE — archived row correctly saved.
            assert rows[1]["archived"] is True
            assert rows[1]["status"] == "read"

            # Row 3: status='reading', flags remain FALSE — saved via DEFAULT backfill.
            assert rows[2]["starred"] is False
            assert rows[2]["archived"] is False
            assert rows[2]["status"] == "reading"

            # CHECK constraint must reject status='starred' post-migration.
            await conn.execute(
                """
                INSERT INTO papers (external_id, source_type, title, authors, url)
                VALUES ('live-pg-046-check', 'arxiv', 'Check 046', ARRAY['Tester'],
                        'https://example.test/046-check')
                """
            )
            check_paper_id = await conn.fetchval(
                "SELECT id FROM papers WHERE external_id = $1",
                "live-pg-046-check",
            )
            with pytest.raises(asyncpg.exceptions.CheckViolationError):
                await conn.execute(
                    """
                    INSERT INTO paper_user_state (paper_id, user_id, status)
                    VALUES ($1, 99, 'starred')
                    """,
                    check_paper_id,
                )

            # Idempotency: re-applying the backfill UPDATE is a no-op on already-correct rows.
            result = await conn.execute(
                "UPDATE paper_user_state SET saved = TRUE WHERE saved IS NOT TRUE"
            )
            # Expect "UPDATE 0" — no rows changed.
            assert result == "UPDATE 0", f"Idempotent re-run should update 0 rows, got: {result}"

    finally:
        await pool.close()
