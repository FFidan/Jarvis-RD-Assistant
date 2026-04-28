"""Live PostgreSQL tests for fresh-init plus migration replay."""

from __future__ import annotations

from pathlib import Path

import asyncpg
import pytest
from paper_ingestion.migrations_runner import run_migrations

pytestmark = pytest.mark.live_pg

_REPO_ROOT = Path(__file__).resolve().parents[3]
_INIT_SQL = _REPO_ROOT / "db" / "init.sql"


async def _apply_fresh_init(pool: asyncpg.Pool) -> None:
    """Apply the same init.sql that Docker runs for a brand-new database volume."""
    async with pool.acquire() as conn:
        await conn.execute(_INIT_SQL.read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_fresh_init_then_migrations_are_idempotent(live_pg_dsn: str) -> None:
    """Fresh boot path: init.sql, run_migrations(), then run_migrations() again."""
    pool = await asyncpg.create_pool(live_pg_dsn, min_size=1, max_size=2)
    try:
        await _apply_fresh_init(pool)
        await run_migrations(pool)
        await run_migrations(pool)

        async with pool.acquire() as conn:
            migration_count = await conn.fetchval("SELECT COUNT(*) FROM schema_migrations")
            latest_version = await conn.fetchval("SELECT MAX(version) FROM schema_migrations")

        assert migration_count == 43
        assert latest_version == 43
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_fresh_boot_migration_043_uniqueness_semantics(live_pg_dsn: str) -> None:
    """Migration 043 constraints allow per-user rows and reject duplicate NULL owners."""
    pool = await asyncpg.create_pool(live_pg_dsn, min_size=1, max_size=2)
    try:
        await _apply_fresh_init(pool)
        await run_migrations(pool)
        await run_migrations(pool)

        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO papers (external_id, source_type, title, authors, url)
                VALUES ('live-pg-043', 'arxiv', 'Live PG 043', ARRAY['Tester'], 'https://example.test')
                """
            )
            paper_id = await conn.fetchval(
                "SELECT id FROM papers WHERE external_id = $1",
                "live-pg-043",
            )

            await conn.execute(
                "INSERT INTO paper_user_state (paper_id, user_id, status) VALUES ($1, NULL, 'new')",
                paper_id,
            )
            await conn.execute(
                "INSERT INTO paper_user_state (paper_id, user_id, status) VALUES ($1, 42, 'new')",
                paper_id,
            )
            with pytest.raises(asyncpg.UniqueViolationError):
                await conn.execute(
                    "INSERT INTO paper_user_state (paper_id, user_id, status) VALUES ($1, NULL, 'new')",
                    paper_id,
                )

            await conn.execute(
                """
                INSERT INTO paper_summaries
                    (paper_id, user_id, summary_brief, summary_detailed, key_findings)
                VALUES ($1, NULL, 'brief', 'detailed', '[]'::jsonb)
                """,
                paper_id,
            )
            await conn.execute(
                """
                INSERT INTO paper_summaries
                    (paper_id, user_id, summary_brief, summary_detailed, key_findings)
                VALUES ($1, 42, 'brief', 'detailed', '[]'::jsonb)
                """,
                paper_id,
            )
            with pytest.raises(asyncpg.UniqueViolationError):
                await conn.execute(
                    """
                    INSERT INTO paper_summaries
                        (paper_id, user_id, summary_brief, summary_detailed, key_findings)
                    VALUES ($1, NULL, 'brief', 'detailed', '[]'::jsonb)
                    """,
                    paper_id,
                )

            await conn.execute(
                "INSERT INTO pulse_decks (deck_date, user_id) VALUES ('2026-04-28', NULL)"
            )
            await conn.execute(
                "INSERT INTO pulse_decks (deck_date, user_id) VALUES ('2026-04-28', 42)"
            )
            with pytest.raises(asyncpg.UniqueViolationError):
                await conn.execute(
                    "INSERT INTO pulse_decks (deck_date, user_id) VALUES ('2026-04-28', NULL)"
                )
    finally:
        await pool.close()
