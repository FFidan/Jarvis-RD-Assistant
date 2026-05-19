"""Structural tests for migration 044 paper_user_state flag split."""

from __future__ import annotations

from pathlib import Path

import asyncpg
import pytest

MIGRATION = Path(__file__).resolve().parents[3] / "db/migrations/044_paper_user_state_flags.sql"


def test_migration_044_adds_state_flags_and_preference() -> None:
    """Migration 044 must add per-user state fields without touching papers."""
    sql = MIGRATION.read_text(encoding="utf-8")

    assert "ALTER TABLE paper_user_state" in sql
    assert "ADD COLUMN IF NOT EXISTS starred BOOLEAN NOT NULL DEFAULT FALSE" in sql
    assert "ADD COLUMN IF NOT EXISTS archived BOOLEAN NOT NULL DEFAULT FALSE" in sql
    assert "ADD COLUMN IF NOT EXISTS preference VARCHAR(10) NOT NULL DEFAULT 'none'" in sql
    assert "preference IN ('none', 'up', 'down')" in sql
    assert "ALTER TABLE papers" not in sql


def test_migration_044_backfills_legacy_statuses() -> None:
    """Legacy status values should survive as compatibility flags."""
    sql = MIGRATION.read_text(encoding="utf-8")

    assert "WHERE status = 'starred'" in sql
    assert "WHERE status = 'archived'" in sql


# ---------------------------------------------------------------------------
# Live-PG marker-gated tests (Sprint 7 B18). Each test carries the
# `live_pg` marker so the text-only tests above continue to run on hosts
# without Docker.
# ---------------------------------------------------------------------------

from tests.migration_helpers import apply_fresh_init as _apply_fresh_init


@pytest.mark.live_pg
@pytest.mark.asyncio
async def test_migration_044_columns_exist_with_correct_types(live_pg_dsn: str) -> None:
    # init.sql + migrations runner applied the migration; verify the on-disk
    # column shape matches what the migration declares.
    from paper_ingestion.migrations_runner import run_migrations

    pool = await asyncpg.create_pool(live_pg_dsn, min_size=1, max_size=2)
    try:
        await _apply_fresh_init(pool)
        await run_migrations(pool)

        async with pool.acquire() as conn:
            cols = {
                row["column_name"]: (row["data_type"], row["column_default"])
                for row in await conn.fetch(
                    """
                    SELECT column_name, data_type, column_default
                    FROM information_schema.columns
                    WHERE table_name = 'paper_user_state'
                      AND column_name IN ('starred', 'archived', 'preference')
                    """
                )
            }
        assert cols["starred"] == ("boolean", "false")
        assert cols["archived"] == ("boolean", "false")
        assert cols["preference"][0] == "character varying"
        assert cols["preference"][1] == "'none'::character varying"
    finally:
        await pool.close()


@pytest.mark.live_pg
@pytest.mark.asyncio
async def test_migration_044_backfill_flips_legacy_status_starred(
    live_pg_dsn: str,
) -> None:
    # Recreate a pre-Sprint-6 row by inserting status='starred' with the new
    # boolean still FALSE, then re-apply the backfill UPDATE statements and
    # assert the flag flipped. Mirrors what would happen on a deployment that
    # already had legacy `status='starred'` rows when migration 044 lands.
    from paper_ingestion.migrations_runner import run_migrations

    pool = await asyncpg.create_pool(live_pg_dsn, min_size=1, max_size=2)
    try:
        await _apply_fresh_init(pool)
        await run_migrations(pool)

        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO papers (external_id, source_type, title, authors, url)
                VALUES ('live-pg-044', 'arxiv', 'Live PG 044', ARRAY['Tester'],
                        'https://example.test/044')
                """
            )
            paper_id = await conn.fetchval(
                "SELECT id FROM papers WHERE external_id = $1",
                "live-pg-044",
            )
            await conn.execute(
                """
                INSERT INTO paper_user_state
                    (paper_id, user_id, status, starred, archived)
                VALUES ($1, NULL, 'starred', FALSE, FALSE)
                """,
                paper_id,
            )
            # Re-apply the backfill statements (idempotent).
            await conn.execute(
                "UPDATE paper_user_state SET starred = TRUE WHERE status = 'starred'"
            )
            row = await conn.fetchrow(
                "SELECT starred, archived FROM paper_user_state WHERE paper_id = $1",
                paper_id,
            )
            assert row["starred"] is True
            assert row["archived"] is False
    finally:
        await pool.close()


@pytest.mark.live_pg
@pytest.mark.asyncio
async def test_migration_044_preference_check_constraint_rejects_invalid_value(
    live_pg_dsn: str,
) -> None:
    from paper_ingestion.migrations_runner import run_migrations

    pool = await asyncpg.create_pool(live_pg_dsn, min_size=1, max_size=2)
    try:
        await _apply_fresh_init(pool)
        await run_migrations(pool)

        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO papers (external_id, source_type, title, authors, url)
                VALUES ('live-pg-044-pref', 'arxiv', 'Pref check', ARRAY['Tester'],
                        'https://example.test/044-pref')
                """
            )
            paper_id = await conn.fetchval(
                "SELECT id FROM papers WHERE external_id = $1",
                "live-pg-044-pref",
            )
            with pytest.raises(asyncpg.exceptions.CheckViolationError):
                await conn.execute(
                    """
                    INSERT INTO paper_user_state
                        (paper_id, user_id, status, preference)
                    VALUES ($1, NULL, 'new', 'maybe')
                    """,
                    paper_id,
                )
    finally:
        await pool.close()
