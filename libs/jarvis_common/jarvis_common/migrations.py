"""Database migration runner shared across JARVIS microservices.

Applies unapplied SQL migrations from a migrations directory on startup using
an advisory transaction lock so concurrent instances don't race.

Originally lived in ``paper_ingestion.migrations_runner``; moved here as part
of the consolidation so non-paper-ingestion services (learning_engine,
future broker workers) can run the same migration logic without depending on
``paper_ingestion``. The old import path is preserved by a thin re-export
shim.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

import asyncpg

logger = logging.getLogger(__name__)

_TXN_LINE_RE = re.compile(r"^\s*(BEGIN|COMMIT|ROLLBACK)\s*;?\s*$", re.IGNORECASE)

# db/init.sql is the full schema baseline through migration 101; db/migrations/
# holds 0102 (WebAuthn credential storage) on top, so the passkey build requires
# schema 102 and the floor below fail-closes a database still stuck at 101.
_MIGRATION_SCHEMA_PROBES: tuple[tuple[int, str, str], ...] = ()

# Used only when db/SCHEMA_VERSION cannot be read (packaging glitch); keep in
# sync with that file, which is the single source of the baseline floor.
_REQUIRED_CODE_SCHEMA_FALLBACK = 102


def _schema_version_path() -> Path:
    """Resolve ``db/SCHEMA_VERSION``: the container path when present, else the
    repo's dev path.

    Mirrors the migrations-directory resolution in ``run_migrations`` so both
    read from the same baseline regardless of where the code runs.
    """
    container = Path("/app/db/SCHEMA_VERSION")
    if container.exists():
        return container
    return Path(__file__).resolve().parents[3] / "db" / "SCHEMA_VERSION"


def required_code_schema() -> int:
    """Return the minimum schema version this build requires to run.

    Reads ``db/SCHEMA_VERSION`` — the single source of the baseline floor,
    bumped only when migrations are next squashed into ``init.sql``. Falls back
    to a module constant (with a warning) when that file is missing or
    unparseable, so a packaging glitch degrades to the known floor rather than
    crashing startup.
    """
    path = _schema_version_path()
    try:
        return int(path.read_text().strip())
    except FileNotFoundError:
        # The file is not shipped into every image; there the app falls back to
        # the in-code baseline. Absence is expected, not a misconfiguration, so
        # it must not emit a warning on every boot.
        return _REQUIRED_CODE_SCHEMA_FALLBACK
    except (OSError, ValueError):
        logger.warning(
            "could not read %s; using built-in schema floor %d",
            path,
            _REQUIRED_CODE_SCHEMA_FALLBACK,
        )
        return _REQUIRED_CODE_SCHEMA_FALLBACK


async def _assert_schema_floor(
    conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
) -> None:
    """Fail closed when the live schema is below the baseline this build requires.

    Runs after the apply loop inside the migration transaction. A fresh install
    premarks 1..N via ``init.sql`` before this runs, so it passes; only a
    genuinely under-baseline database fails — loudly, before serving traffic.
    """
    floor = required_code_schema()
    max_applied = int(
        await conn.fetchval("SELECT COALESCE(MAX(version), 0) FROM schema_migrations") or 0
    )
    if max_applied < floor:
        raise RuntimeError(
            f"refusing to start: database schema is at version {max_applied}, but this "
            f"build requires at least {floor}. Apply the missing migrations or restore "
            "from a compatible backup."
        )


def _strip_outer_transaction_control(sql: str) -> str:
    """Drop standalone BEGIN/COMMIT/ROLLBACK lines outside dollar-quoted blocks.

    PL/pgSQL function bodies (`CREATE FUNCTION ... AS $$ BEGIN ... END $$`) and
    DO blocks (`DO $$ BEGIN ... END $$`) have their own bare `BEGIN`/`END;` that
    must not be stripped — only the outer transaction-control statements should.

    Uses a character-level state machine so that a ``$$`` open and close on the
    same line (e.g. ``DO $$ BEGIN END $$;``) toggles the flag twice and ends up
    outside a dollar-quoted block — the old line-count approach got this right
    by accident only when the line had an even number of ``$$`` tokens, but
    split the "chunk outside dollar" check from the "line-to-filter" decision
    in a way that could incorrectly suppress transaction-control keywords that
    appear in the same source line as an inline dollar-quoted block.
    """
    pieces: list[str] = []
    in_dollar = False
    i = 0
    n = len(sql)
    while i < n:
        # Find the next ``$$`` that is a real delimiter. A ``$$`` sitting inside
        # a ``--`` line comment (e.g. migration 080's "DO $$ ... guard" doc
        # comment) is NOT a delimiter — counting it would invert the in_dollar
        # polarity and cause a real DO block to be parsed as outer SQL, stripping
        # its standalone ``BEGIN``. Comment-skipping only applies outside a
        # dollar block: once inside one, the next ``$$`` always closes it.
        dollar_idx = -1
        j = i
        while j < n:
            if not in_dollar and sql.startswith("--", j):
                nl = sql.find("\n", j)
                if nl == -1:
                    j = n
                    break
                j = nl + 1
                continue
            if sql.startswith("$$", j):
                dollar_idx = j
                break
            j += 1
        if dollar_idx == -1:
            chunk = sql[i:]
            i = n
        else:
            chunk = sql[i:dollar_idx]
            i = dollar_idx + 2
        if not in_dollar:
            kept_lines = [line for line in chunk.split("\n") if not _TXN_LINE_RE.match(line)]
            pieces.append("\n".join(kept_lines))
        else:
            pieces.append(chunk)
        if dollar_idx != -1:
            # Re-emit the $$ delimiter we consumed during scanning
            pieces.append("$$")
            in_dollar = not in_dollar
    return "".join(pieces)


async def _repair_false_applied_migrations(
    conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
) -> None:
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


async def run_migrations(
    pool: asyncpg.Pool,
    migrations_dir: Path | None = None,
) -> None:
    """Apply unapplied SQL migrations from a migrations directory on startup.

    Holds a Postgres advisory transaction lock (key 42) so concurrent service
    replicas do not race.  Each migration file is applied inside a savepoint;
    ``BEGIN``/``COMMIT``/``ROLLBACK`` lines at the outer transaction level are
    stripped to avoid conflicts with asyncpg's own transaction wrapping.

    Parameters
    ----------
    pool:
        asyncpg connection pool to run migrations against.
    migrations_dir:
        Directory containing ``NNN_*.sql`` migration files.  When ``None``,
        defaults to ``/app/db/migrations`` (in-container), falling back to
        ``parents[3] / "db" / "migrations"`` for local dev.  Pass an
        explicit path when calling from services other than ``paper_ingestion``
        so the resolution is unambiguous.

    Raises
    ------
    RuntimeError
        If another instance holds the migration lock and
        ``JARVIS_MIGRATION_LOCK_CONTENDED_OK`` is not set, or if a duplicate
        migration version number is detected in the directory.

    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Bound the advisory-lock wait so a crashed holder never stalls startup.
            await conn.execute("SET LOCAL lock_timeout = '60s'")
            try:
                await conn.execute("SELECT pg_advisory_xact_lock(42)")
            except asyncpg.PostgresError as exc:
                if getattr(exc, "sqlstate", None) != "55P03":
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

            if migrations_dir is None:
                migrations_dir = Path("/app/db/migrations")
                if not migrations_dir.exists():
                    # Fallback for local dev. ``parents[3]`` resolves the repo
                    # root from ``libs/jarvis_common/jarvis_common/migrations.py``;
                    # the same arithmetic also worked from the old paper_ingestion
                    # location, so existing dev workflows keep functioning.
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

            # Still inside the advisory-locked transaction: refuse to serve on a
            # schema below the baseline this build was packaged with.
            await _assert_schema_floor(conn)
