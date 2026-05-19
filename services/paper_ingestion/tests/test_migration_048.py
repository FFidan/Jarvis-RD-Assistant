"""Structural and live-PG tests for migration 048 (papers.discovery_origin)."""

from __future__ import annotations

from pathlib import Path

import asyncpg
import pytest

MIGRATION = Path(__file__).resolve().parents[3] / "db/migrations/048_papers_discovery_origin.sql"


def test_migration_048_text() -> None:
    """Migration 048 SQL must contain all expected DDL/DML snippets."""
    sql = MIGRATION.read_text(encoding="utf-8")

    assert "ADD COLUMN IF NOT EXISTS discovery_origin TEXT NOT NULL DEFAULT 'user_initiated'" in sql
    assert (
        "CHECK (discovery_origin IN ('user_initiated', 'pulse', 'recommender', 'citation_batch'))"
        in sql
    )
    # Backfill ordering: pulse_cards → 'pulse'; paper_recommendations → 'recommender'.
    assert "UPDATE papers SET discovery_origin = 'pulse'" in sql
    assert "UPDATE papers SET discovery_origin = 'recommender'" in sql
    assert "FROM pulse_cards" in sql
    assert "FROM paper_recommendations" in sql
    assert "CREATE INDEX IF NOT EXISTS idx_papers_discovery_origin" in sql


from tests.migration_helpers import apply_fresh_init as _apply_fresh_init


@pytest.mark.live_pg
@pytest.mark.asyncio
async def test_migration_048_live_pg(live_pg_dsn: str) -> None:
    """Apply migrations through 048 and verify discovery_origin column shape."""
    from paper_ingestion.migrations_runner import run_migrations

    pool = await asyncpg.create_pool(live_pg_dsn, min_size=1, max_size=2)
    try:
        await _apply_fresh_init(pool)
        await run_migrations(pool)

        async with pool.acquire() as conn:
            # Insert four papers with each origin value.
            for ext_id, origin in [
                ("live-pg-048-ui", "user_initiated"),
                ("live-pg-048-p", "pulse"),
                ("live-pg-048-r", "recommender"),
                ("live-pg-048-c", "citation_batch"),
            ]:
                await conn.execute(
                    """
                    INSERT INTO papers (external_id, source_type, title, authors, url, discovery_origin)
                    VALUES ($1, 'arxiv', 'Live PG 048', ARRAY['Tester'], $2, $3)
                    """,
                    ext_id,
                    f"https://example.test/{ext_id}",
                    origin,
                )

            rows = await conn.fetch(
                "SELECT external_id, discovery_origin FROM papers "
                "WHERE external_id LIKE 'live-pg-048-%' ORDER BY external_id"
            )
            origins = {r["external_id"]: r["discovery_origin"] for r in rows}
            assert origins["live-pg-048-c"] == "citation_batch"
            assert origins["live-pg-048-p"] == "pulse"
            assert origins["live-pg-048-r"] == "recommender"
            assert origins["live-pg-048-ui"] == "user_initiated"

            # Default value: papers inserted without origin should be 'user_initiated'.
            await conn.execute(
                """
                INSERT INTO papers (external_id, source_type, title, authors, url)
                VALUES ('live-pg-048-default', 'arxiv', 'Default', ARRAY['Tester'],
                        'https://example.test/048-default')
                """
            )
            default = await conn.fetchval(
                "SELECT discovery_origin FROM papers WHERE external_id = $1",
                "live-pg-048-default",
            )
            assert default == "user_initiated"

            # Invalid origin must be rejected.
            with pytest.raises(asyncpg.exceptions.CheckViolationError):
                await conn.execute(
                    """
                    INSERT INTO papers (external_id, source_type, title, authors, url, discovery_origin)
                    VALUES ('live-pg-048-bad', 'arxiv', 'Bad', ARRAY['Tester'],
                            'https://example.test/048-bad', 'rss_feed')
                    """
                )

    finally:
        await pool.close()
