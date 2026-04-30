"""Structural and live-PG tests for migration 049 (recommendation_feedback table + drop pulse_ratings)."""

from __future__ import annotations

from pathlib import Path

import asyncpg
import pytest

MIGRATION = Path(__file__).resolve().parents[3] / "db/migrations/049_recommendation_feedback.sql"


def test_migration_049_text() -> None:
    """Migration 049 SQL must contain all expected DDL/DML snippets."""
    sql = MIGRATION.read_text(encoding="utf-8")

    # Table creation with constraints.
    assert "CREATE TABLE IF NOT EXISTS recommendation_feedback" in sql
    assert "signal          TEXT NOT NULL CHECK (signal IN ('positive', 'negative'))" in sql
    assert "'pulse_thumbs'" in sql
    assert "'feed_thumbs'" in sql
    assert "'paper_detail_thumbs'" in sql
    assert "'dismiss_combined'" in sql
    # UNIQUE NULLS NOT DISTINCT lets single-tenant (user_id IS NULL) rows still upsert.
    assert "UNIQUE NULLS NOT DISTINCT (paper_id, user_id, source)" in sql

    # Indexes.
    assert "recommendation_feedback_paper_idx" in sql
    assert "recommendation_feedback_signal_recent_idx" in sql
    assert "recommendation_feedback_topic_idx" in sql

    # Migration from pulse_ratings (skip 'save' and 'open').
    assert "FROM pulse_ratings pr" in sql
    assert "WHERE pr.rating IN ('up', 'down', 'dismiss')" in sql

    # Pulse_ratings DROP.
    assert "DROP TABLE IF EXISTS pulse_ratings" in sql


_REPO_ROOT = Path(__file__).resolve().parents[3]
_INIT_SQL = _REPO_ROOT / "db" / "init.sql"


async def _apply_fresh_init(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(_INIT_SQL.read_text(encoding="utf-8"))


@pytest.mark.live_pg
@pytest.mark.asyncio
async def test_migration_049_live_pg(live_pg_dsn: str) -> None:
    """Apply migrations through 049 and verify recommendation_feedback shape + pulse_ratings dropped."""
    from paper_ingestion.migrations_runner import run_migrations

    pool = await asyncpg.create_pool(live_pg_dsn, min_size=1, max_size=2)
    try:
        await _apply_fresh_init(pool)
        await run_migrations(pool)

        async with pool.acquire() as conn:
            # pulse_ratings table must be DROPPED.
            exists = await conn.fetchval(
                """
                SELECT EXISTS(
                    SELECT 1 FROM information_schema.tables
                     WHERE table_schema = 'public' AND table_name = 'pulse_ratings'
                )
                """
            )
            assert exists is False, "pulse_ratings table must be dropped by migration 049"

            # recommendation_feedback table must exist.
            exists = await conn.fetchval(
                """
                SELECT EXISTS(
                    SELECT 1 FROM information_schema.tables
                     WHERE table_schema = 'public' AND table_name = 'recommendation_feedback'
                )
                """
            )
            assert exists is True, "recommendation_feedback table must be created"

            # Insert a paper to anchor feedback rows.
            await conn.execute(
                """
                INSERT INTO papers (external_id, source_type, title, authors, url, discovery_origin)
                VALUES ('live-pg-049', 'arxiv', 'Live PG 049', ARRAY['Tester'],
                        'https://example.test/049', 'pulse')
                """
            )
            paper_id = await conn.fetchval(
                "SELECT id FROM papers WHERE external_id = $1",
                "live-pg-049",
            )

            # Valid signal+source insert succeeds.
            await conn.execute(
                """
                INSERT INTO recommendation_feedback (paper_id, user_id, signal, source)
                VALUES ($1, NULL, 'negative', 'pulse_thumbs')
                """,
                paper_id,
            )
            cnt = await conn.fetchval(
                "SELECT COUNT(*) FROM recommendation_feedback WHERE paper_id = $1",
                paper_id,
            )
            assert cnt == 1

            # Invalid signal value must be rejected.
            with pytest.raises(asyncpg.exceptions.CheckViolationError):
                await conn.execute(
                    """
                    INSERT INTO recommendation_feedback (paper_id, user_id, signal, source)
                    VALUES ($1, 1, 'neutral', 'pulse_thumbs')
                    """,
                    paper_id,
                )

            # Invalid source value must be rejected.
            with pytest.raises(asyncpg.exceptions.CheckViolationError):
                await conn.execute(
                    """
                    INSERT INTO recommendation_feedback (paper_id, user_id, signal, source)
                    VALUES ($1, 1, 'positive', 'rss_thumb')
                    """,
                    paper_id,
                )

            # UNIQUE (paper_id, user_id, source) — duplicate insert via upsert ON CONFLICT.
            await conn.execute(
                """
                INSERT INTO recommendation_feedback (paper_id, user_id, signal, source)
                VALUES ($1, NULL, 'positive', 'pulse_thumbs')
                ON CONFLICT (paper_id, user_id, source) DO UPDATE
                    SET signal = EXCLUDED.signal, created_at = NOW()
                """,
                paper_id,
            )
            signal = await conn.fetchval(
                """
                SELECT signal FROM recommendation_feedback
                 WHERE paper_id = $1 AND user_id IS NULL AND source = 'pulse_thumbs'
                """,
                paper_id,
            )
            assert signal == "positive", "Upsert must replace the prior 'negative' signal"

    finally:
        await pool.close()
