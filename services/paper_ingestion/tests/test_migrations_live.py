"""Live PostgreSQL tests for fresh-init plus migration replay."""

from __future__ import annotations

from pathlib import Path

import asyncpg
import pytest
from paper_ingestion.migrations_runner import run_migrations

pytestmark = pytest.mark.live_pg

_REPO_ROOT = Path(__file__).resolve().parents[3]
_INIT_SQL = _REPO_ROOT / "db" / "init.sql"
_MIGRATIONS_DIR = _REPO_ROOT / "db" / "migrations"


def _migration_versions() -> set[int]:
    versions: set[int] = set()
    for sql_file in _MIGRATIONS_DIR.glob("*.sql"):
        try:
            versions.add(int(sql_file.name.split("_", maxsplit=1)[0]))
        except (IndexError, ValueError):
            continue
    return versions


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

        versions = _migration_versions()
        assert migration_count == len(versions)
        assert latest_version == max(versions)
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
                "INSERT INTO paper_user_state (paper_id, user_id, state) VALUES ($1, NULL, 'inbox')",
                paper_id,
            )
            await conn.execute(
                "INSERT INTO paper_user_state (paper_id, user_id, state) VALUES ($1, 42, 'inbox')",
                paper_id,
            )
            with pytest.raises(asyncpg.UniqueViolationError):
                await conn.execute(
                    "INSERT INTO paper_user_state (paper_id, user_id, state) VALUES ($1, NULL, 'inbox')",
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


@pytest.mark.asyncio
async def test_false_applied_rows_are_repaired_and_replayed(live_pg_dsn: str) -> None:
    """Old init.sql snapshots may have marked migrations applied without schema evidence."""
    pool = await asyncpg.create_pool(live_pg_dsn, min_size=1, max_size=2)
    try:
        await _apply_fresh_init(pool)

        async with pool.acquire() as conn:
            await conn.executemany(
                "INSERT INTO schema_migrations (version) VALUES ($1) ON CONFLICT DO NOTHING",
                [(33,), (52,), (54,)],
            )

        await run_migrations(pool)
        await run_migrations(pool)

        async with pool.acquire() as conn:
            encrypted_value_exists = await conn.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'user_config'
                      AND column_name = 'encrypted_value'
                )
                """
            )
            procrastinate_exists = await conn.fetchval(
                """
                SELECT
                    to_regtype('public.procrastinate_job_to_defer_v1') IS NOT NULL
                    AND to_regclass('public.procrastinate_jobs') IS NOT NULL
                    AND EXISTS (
                        SELECT 1 FROM pg_proc
                        WHERE proname = 'procrastinate_defer_jobs_v1'
                    )
                """
            )
            job_progress_exists = await conn.fetchval(
                "SELECT to_regclass('public.job_progress') IS NOT NULL"
            )
            applied_versions = {
                row["version"] for row in await conn.fetch("SELECT version FROM schema_migrations")
            }

        versions = _migration_versions()
        assert encrypted_value_exists is True
        assert procrastinate_exists is True
        assert job_progress_exists is True
        assert applied_versions == versions
    finally:
        await pool.close()
