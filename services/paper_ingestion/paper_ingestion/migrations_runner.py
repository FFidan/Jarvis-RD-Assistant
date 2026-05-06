"""Database migration runner for the paper_ingestion service.

Applies unapplied SQL migrations from db/migrations/ on startup using an
advisory transaction lock so concurrent instances don't race.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

import asyncpg

logger = logging.getLogger(__name__)

_TXN_LINE_RE = re.compile(r"^\s*(BEGIN|COMMIT|ROLLBACK)\s*;?\s*$", re.IGNORECASE)

_MIGRATION_SCHEMA_PROBES: tuple[tuple[int, str, str], ...] = (
    (
        33,
        "user_config.encrypted_value",
        """
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'user_config'
              AND column_name = 'encrypted_value'
        )
        """,
    ),
    (
        49,
        "recommendation_feedback table with legacy pulse_ratings dropped",
        """
        SELECT
            to_regclass('public.recommendation_feedback') IS NOT NULL
            AND to_regclass('public.pulse_ratings') IS NULL
        """,
    ),
    (
        50,
        "My Day phase 2 columns",
        """
        SELECT
            EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'projects'
                  AND column_name = 'next_milestone'
            )
            AND EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'projects'
                  AND column_name = 'next_milestone_due'
            )
            AND EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'tasks'
                  AND column_name = 'color'
            )
        """,
    ),
    (51, "journal_entries table", "SELECT to_regclass('public.journal_entries') IS NOT NULL"),
    (
        52,
        "Procrastinate broker schema",
        """
        SELECT
            to_regtype('public.procrastinate_job_to_defer_v1') IS NOT NULL
            AND to_regclass('public.procrastinate_jobs') IS NOT NULL
            AND EXISTS (
                SELECT 1
                FROM pg_proc
                WHERE proname = 'procrastinate_defer_jobs_v1'
            )
        """,
    ),
    (53, "legacy jobs table removal", "SELECT to_regclass('public.jobs') IS NULL"),
    (54, "job_progress table", "SELECT to_regclass('public.job_progress') IS NOT NULL"),
    (
        55,
        "recommendation_feedback.user_id integer type",
        """
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'recommendation_feedback'
              AND column_name = 'user_id'
              AND udt_name = 'int4'
        )
        """,
    ),
    (
        57,
        "default model and FSRS config rows",
        """
        SELECT NOT EXISTS (
            SELECT 1
            FROM (
                VALUES
                    ('llm.smart_model'),
                    ('llm.fast_model'),
                    ('llm.embed_model'),
                    ('fsrs.desired_retention'),
                    ('fsrs.learning_steps')
            ) AS required(key)
            WHERE NOT EXISTS (
                SELECT 1
                FROM user_config
                WHERE user_config.key = required.key
            )
        )
        """,
    ),
    (
        58,
        "job_progress terminal result/error columns",
        """
        SELECT
            EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'job_progress'
                  AND column_name = 'result'
            )
            AND EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'job_progress'
                  AND column_name = 'error'
            )
        """,
    ),
)


def _strip_outer_transaction_control(sql: str) -> str:
    """Drop standalone BEGIN/COMMIT/ROLLBACK lines outside dollar-quoted blocks.

    PL/pgSQL function bodies (`CREATE FUNCTION ... AS $$ BEGIN ... END $$`) and
    DO blocks (`DO $$ BEGIN ... END $$`) have their own bare `BEGIN`/`END;` that
    must not be stripped — only the outer transaction-control statements should.
    """
    out: list[str] = []
    in_dollar = False
    for line in sql.splitlines(keepends=True):
        # Toggle on every `$$` occurrence in the line. Migrations in this repo
        # use bare `$$` (no tagged dollar-quotes); revisit if `$tag$` appears.
        for _ in range(line.count("$$")):
            in_dollar = not in_dollar
        if not in_dollar and _TXN_LINE_RE.match(line):
            continue
        out.append(line)
    return "".join(out)


async def _repair_false_applied_migrations(conn: asyncpg.Connection) -> None:
    """Remove known false-applied markers when their schema probe fails."""
    probe_versions = [version for version, _, _ in _MIGRATION_SCHEMA_PROBES]
    applied = {
        row["version"]
        for row in await conn.fetch(
            "SELECT version FROM schema_migrations WHERE version = ANY($1::int[])",
            probe_versions,
        )
    }

    for version, description, probe_sql in _MIGRATION_SCHEMA_PROBES:
        if version not in applied:
            continue

        try:
            probe_ok = await conn.fetchval(probe_sql)
        except (
            asyncpg.UndefinedColumnError,
            asyncpg.UndefinedObjectError,
            asyncpg.UndefinedTableError,
        ):
            probe_ok = False
        if probe_ok:
            continue

        logger.warning(
            "schema_migrations marks migration %s as applied but %s is missing; "
            "removing marker so the migration can replay",
            version,
            description,
        )
        await conn.execute("DELETE FROM schema_migrations WHERE version = $1", version)


async def run_migrations(pool: asyncpg.Pool) -> None:
    """Apply unapplied SQL migrations from db/migrations/ on startup."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Bound the advisory-lock wait so a crashed holder never stalls startup.
            await conn.execute("SET LOCAL lock_timeout = '60s'")
            try:
                await conn.execute("SELECT pg_advisory_xact_lock(42)")
            except (asyncpg.LockNotAvailableError, asyncpg.PostgresError) as exc:
                is_lock_timeout = not isinstance(exc, asyncpg.PostgresError) or (
                    getattr(exc, "sqlstate", None) == "55P03"
                )
                if not is_lock_timeout:
                    raise  # Not a lock-timeout error — let it propagate
                message = "migration lock contended — another instance is running migrations"
                if os.environ.get("JARVIS_MIGRATION_LOCK_CONTENDED_OK", "").lower() in {
                    "1",
                    "true",
                    "yes",
                }:
                    logger.warning("%s; skipping because compatibility flag is set", message)
                    return
                raise RuntimeError(f"{message}; refusing to start with unverified schema") from None
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            await _repair_false_applied_migrations(conn)
            applied = {
                r["version"] for r in await conn.fetch("SELECT version FROM schema_migrations")
            }

            migrations_dir = Path("/app/db/migrations")
            if not migrations_dir.exists():
                # Fallback for local dev
                migrations_dir = Path(__file__).resolve().parents[3] / "db" / "migrations"
            if not migrations_dir.exists():
                logger.warning("Migrations directory not found, skipping migrations")
                return

            # Detect version collisions before applying anything — fail loudly so
            # CI catches duplicates rather than silently dropping migrations.
            seen_versions: dict[int, str] = {}
            for sql_file in sorted(migrations_dir.glob("*.sql")):
                try:
                    ver = int(sql_file.name.split("_")[0])
                except (ValueError, IndexError):
                    continue  # non-migration file — skipped below
                if ver in seen_versions:
                    raise RuntimeError(
                        f"duplicate migration version: {ver} "
                        f"({sql_file.name} vs {seen_versions[ver]})"
                    )
                seen_versions[ver] = sql_file.name

            for sql_file in sorted(migrations_dir.glob("*.sql")):
                try:
                    version = int(sql_file.name.split("_")[0])
                except (ValueError, IndexError):
                    logger.warning("Skipping non-migration file: %s", sql_file.name)
                    continue
                if version in applied:
                    continue
                logger.info("Applying migration %s: %s", version, sql_file.name)
                sql = sql_file.read_text()
                # Strip standalone BEGIN/COMMIT/ROLLBACK lines so they don't
                # conflict with the outer asyncpg transaction (savepoint) wrapper.
                # asyncpg runs each migration inside a savepoint; nested explicit
                # transaction commands cause "can't run BEGIN inside a transaction".
                # Skip stripping inside $$-quoted blocks (PL/pgSQL function bodies
                # and DO blocks legitimately use `BEGIN`/`END` on their own lines).
                cleaned_sql = _strip_outer_transaction_control(sql)
                async with conn.transaction():
                    await conn.execute(cleaned_sql)
                    await conn.execute(
                        "INSERT INTO schema_migrations (version) VALUES ($1)", version
                    )
                logger.info("Migration %s applied successfully", version)
