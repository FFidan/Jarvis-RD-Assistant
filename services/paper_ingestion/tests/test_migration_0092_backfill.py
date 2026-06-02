"""Live-PG tests for migration 0092 (backfill NULL user_id → single admin).

Each test gets its OWN disposable postgres:16.8 container (function-scoped
``live_pg_dsn``) so it can control the exact ordering:

    1. apply db/init.sql            (schema + schema_migrations seeded 1..91)
    2. seed users + NULL-user rows  (BEFORE 0092 has run)
    3. run_migrations()             (applies the one on-disk migration: 0092)
    4. assert the backfill outcome

This is the only ordering that exercises 0092's logic: the session-scoped
contract / baseline pools run ``run_migrations`` at setup, so 0092 would
already be applied before a test could seed its NULL rows.

Cases:
  1. one admin  → NULLs re-owned; daily_log counts SUM-merged (COALESCE path
     exercised); colliding Class-B NULL orphan deleted, admin row intact.
  2. two admins → guard fires, NULLs REMAIN (no-op).
  3. zero admins → guard fires, NULLs REMAIN (no-op).

Gated by JARVIS_RUN_LIVE_PG=1 via the ``live_pg`` marker (excluded by the
default addopts), same convention as test_baseline_invariants.py.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import asyncpg
import pytest
from jarvis_common.db_helpers import init_pg_connection
from jarvis_common.migrations import run_migrations
from tests.migration_helpers import apply_fresh_init

pytestmark = pytest.mark.live_pg

_DB_DIR = Path(__file__).resolve().parents[3] / "db"
_MIGRATIONS_DIR = _DB_DIR / "migrations"


async def _make_init_pool(dsn: str) -> asyncpg.Pool:
    """Create a pool against *dsn* and apply db/init.sql (NO migrations yet)."""
    pool = None
    for attempt in range(10):
        try:
            pool = await asyncpg.create_pool(dsn, min_size=1, max_size=3, init=init_pg_connection)
            break
        except (OSError, asyncpg.PostgresError):
            if attempt == 9:
                raise
            await asyncio.sleep(0.5)
    assert pool is not None
    await apply_fresh_init(pool)
    return pool


async def _seed_admin(conn: asyncpg.Connection, email: str) -> int:
    """Insert one active admin user; return its id."""
    return int(
        await conn.fetchval(
            "INSERT INTO users (email, role) VALUES ($1, 'admin') RETURNING id",
            email,
        )
    )


async def _seed_paper(conn: asyncpg.Connection, external_id: str) -> int:
    """Insert a minimal valid papers row; return its id."""
    return int(
        await conn.fetchval(
            """INSERT INTO papers (external_id, source_type, title, authors, url)
               VALUES ($1, 'arxiv', 'P', ARRAY['A'], 'https://example.test')
               RETURNING id""",
            external_id,
        )
    )


# ---------------------------------------------------------------------------
# Case 1 — exactly one admin: backfill runs.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio(loop_scope="session")
async def test_0092_single_admin_reowns_merges_and_dedupes(live_pg_dsn: str) -> None:
    pool = await _make_init_pool(live_pg_dsn)
    try:
        async with pool.acquire() as conn:
            admin_id = await _seed_admin(conn, "admin@example.test")

            # --- Class A: a NULL-user project that should be re-owned ---
            null_project_id = await conn.fetchval(
                "INSERT INTO projects (name, user_id) VALUES ('legacy', NULL) RETURNING id"
            )

            # --- Class B1: daily_log SUM-merge, exercising the COALESCE path ---
            # Admin already has a row for 2026-01-01 with one count column NULL;
            # a colliding NULL-user row has counts. After merge the admin row must
            # hold the SUM and NO column may be NULL.
            await conn.execute(
                """INSERT INTO daily_log
                       (user_id, log_date, tasks_completed, cards_reviewed, papers_read, focus_hours)
                   VALUES ($1, '2026-01-01', 2, NULL, 1, 0.5)""",
                admin_id,
            )
            await conn.execute(
                """INSERT INTO daily_log
                       (user_id, log_date, tasks_completed, cards_reviewed, papers_read, focus_hours)
                   VALUES (NULL, '2026-01-01', 3, 4, NULL, 1.5)"""
            )
            # A NON-colliding NULL daily_log row (different date) → plain re-own.
            await conn.execute(
                """INSERT INTO daily_log
                       (user_id, log_date, tasks_completed, cards_reviewed, papers_read, focus_hours)
                   VALUES (NULL, '2026-02-02', 7, 7, 7, 7.0)"""
            )

            # --- Class B2: paper_summaries delete-orphan-on-collision ---
            paper_id = await _seed_paper(conn, "ext-0092-1")
            await conn.execute(
                """INSERT INTO paper_summaries (paper_id, summary_brief, summary_detailed, user_id)
                   VALUES ($1, 'admin brief', 'admin detail', $2)""",
                paper_id,
                admin_id,
            )
            await conn.execute(
                """INSERT INTO paper_summaries (paper_id, summary_brief, summary_detailed, user_id)
                   VALUES ($1, 'null brief', 'null detail', NULL)""",
                paper_id,
            )

        # --- apply 0092 ---
        await run_migrations(pool, migrations_dir=_MIGRATIONS_DIR)

        async with pool.acquire() as conn:
            # 0092 was actually applied.
            assert await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM schema_migrations WHERE version = 92)"
            ), "migration 0092 was not recorded as applied"

            # Class A: project re-owned, no NULL-user rows remain.
            project_owner = await conn.fetchval(
                "SELECT user_id FROM projects WHERE id = $1", null_project_id
            )
            null_projects = await conn.fetchval(
                "SELECT COUNT(*) FROM projects WHERE user_id IS NULL"
            )
            assert project_owner == admin_id
            assert null_projects == 0

            # Class B1 merge: admin's 2026-01-01 row holds the SUM and NO NULL cols.
            merged = await conn.fetchrow(
                """SELECT tasks_completed, cards_reviewed, papers_read, focus_hours
                   FROM daily_log WHERE user_id = $1 AND log_date = '2026-01-01'""",
                admin_id,
            )
            assert merged["tasks_completed"] == 5  # 2 + 3
            assert merged["cards_reviewed"] == 4  # NULL(→0) + 4  (COALESCE path)
            assert merged["papers_read"] == 1  # 1 + NULL(→0)  (COALESCE path)
            assert merged["focus_hours"] == pytest.approx(2.0)  # 0.5 + 1.5
            # None of the merged counters may be NULL.
            assert all(merged[c] is not None for c in merged.keys())

            # The colliding NULL daily_log row was deleted; the non-colliding one re-owned.
            null_daily = await conn.fetchval("SELECT COUNT(*) FROM daily_log WHERE user_id IS NULL")
            reowned = await conn.fetchrow(
                "SELECT user_id, tasks_completed FROM daily_log WHERE log_date = '2026-02-02'"
            )
            total_daily = await conn.fetchval("SELECT COUNT(*) FROM daily_log")
            assert null_daily == 0
            assert reowned["user_id"] == admin_id
            assert reowned["tasks_completed"] == 7
            assert total_daily == 2  # merged + re-owned

            # Class B2: NULL orphan deleted, admin row intact, no NULL rows remain.
            null_summaries = await conn.fetchval(
                "SELECT COUNT(*) FROM paper_summaries WHERE user_id IS NULL"
            )
            kept_brief = await conn.fetchval(
                "SELECT summary_brief FROM paper_summaries WHERE paper_id = $1 AND user_id = $2",
                paper_id,
                admin_id,
            )
            total_summaries = await conn.fetchval("SELECT COUNT(*) FROM paper_summaries")
            assert null_summaries == 0
            assert kept_brief == "admin brief"
            assert total_summaries == 1
    finally:
        await pool.close()


# ---------------------------------------------------------------------------
# Case 2 — two admins: guard fires, NULLs untouched.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio(loop_scope="session")
async def test_0092_two_admins_is_no_op(live_pg_dsn: str) -> None:
    pool = await _make_init_pool(live_pg_dsn)
    try:
        async with pool.acquire() as conn:
            await _seed_admin(conn, "admin1@example.test")
            await _seed_admin(conn, "admin2@example.test")
            null_project_id = await conn.fetchval(
                "INSERT INTO projects (name, user_id) VALUES ('legacy', NULL) RETURNING id"
            )

        await run_migrations(pool, migrations_dir=_MIGRATIONS_DIR)

        async with pool.acquire() as conn:
            # Guard fired: the NULL row is untouched (still NULL).
            project_owner = await conn.fetchval(
                "SELECT user_id FROM projects WHERE id = $1", null_project_id
            )
            null_projects = await conn.fetchval(
                "SELECT COUNT(*) FROM projects WHERE user_id IS NULL"
            )
            assert project_owner is None
            assert null_projects == 1
    finally:
        await pool.close()


# ---------------------------------------------------------------------------
# Case 3 — zero admins: guard fires, NULLs untouched.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio(loop_scope="session")
async def test_0092_zero_admins_is_no_op(live_pg_dsn: str) -> None:
    pool = await _make_init_pool(live_pg_dsn)
    try:
        async with pool.acquire() as conn:
            # A non-admin user exists, but no admin.
            await conn.execute(
                "INSERT INTO users (email, role) VALUES ('plain@example.test', 'user')"
            )
            null_project_id = await conn.fetchval(
                "INSERT INTO projects (name, user_id) VALUES ('legacy', NULL) RETURNING id"
            )

        await run_migrations(pool, migrations_dir=_MIGRATIONS_DIR)

        async with pool.acquire() as conn:
            project_owner = await conn.fetchval(
                "SELECT user_id FROM projects WHERE id = $1", null_project_id
            )
            null_projects = await conn.fetchval(
                "SELECT COUNT(*) FROM projects WHERE user_id IS NULL"
            )
            assert project_owner is None
            assert null_projects == 1
    finally:
        await pool.close()
