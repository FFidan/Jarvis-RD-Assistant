"""Database migration runner for the paper_ingestion service.

Applies unapplied SQL migrations from db/migrations/ on startup using an
advisory transaction lock so concurrent instances don't race.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import asyncpg

logger = logging.getLogger(__name__)


async def run_migrations(pool: asyncpg.Pool) -> None:
    """Apply unapplied SQL migrations from db/migrations/ on startup."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Bound the advisory-lock wait so a crashed holder never stalls startup.
            await conn.execute("SET LOCAL lock_timeout = '60s'")
            try:
                await conn.execute("SELECT pg_advisory_xact_lock(42)")
            except asyncpg.LockNotAvailableError:
                logger.warning(
                    "migration lock contended — another instance is running migrations; skipping"
                )
                return  # Other instance handles migrations; treat as success
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
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
                cleaned_sql = re.sub(
                    r"^\s*(BEGIN|COMMIT|ROLLBACK)\s*;?\s*$",
                    "",
                    sql,
                    flags=re.IGNORECASE | re.MULTILINE,
                )
                async with conn.transaction():
                    await conn.execute(cleaned_sql)
                    await conn.execute(
                        "INSERT INTO schema_migrations (version) VALUES ($1)", version
                    )
                logger.info("Migration %s applied successfully", version)
