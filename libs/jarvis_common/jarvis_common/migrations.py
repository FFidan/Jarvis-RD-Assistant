"""Database migration runner shared across JARVIS microservices.

Applies unapplied SQL migrations from a migrations directory on startup using
an advisory transaction lock so concurrent instances don't race.

This library-level owner lets every service run the same migration logic
without depending on the paper-ingestion package.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

import asyncpg

logger = logging.getLogger(__name__)

_TXN_LINE_RE = re.compile(r"^\s*(BEGIN|COMMIT|ROLLBACK)\s*;?\s*$", re.IGNORECASE)
_SCHEMA_FLOOR_CONTENTION_TIMEOUT_SECONDS = 5.0
_SCHEMA_FLOOR_CONTENTION_POLL_SECONDS = 0.1

# db/init.sql is the full schema baseline through migration 101; db/migrations/
# holds the required post-baseline migrations. db/SCHEMA_VERSION records the
# highest version shipped; it is not duplicated here.
_MIGRATION_SCHEMA_PROBES: tuple[tuple[int, str, str], ...] = ()

# Used only when db/SCHEMA_VERSION cannot be read (packaging glitch); keep in
# sync with that file, which is the single source of the baseline floor.
_REQUIRED_CODE_SCHEMA_FALLBACK = 118


@dataclass(frozen=True, slots=True)
class MigrationCheck:
    """Read-only schema compatibility result for a runtime database role.

    Attributes
    ----------
    current_user:
        PostgreSQL role used by the checked connection.
    packaged_version:
        Minimum migration revision required by the running build.
    live_version:
        Highest migration revision recorded by PostgreSQL.
    integrity:
        ``"ok"`` when the manifest, applied revisions, and stored hashes all
        match the packaged migration set.
    """

    current_user: str
    packaged_version: int
    live_version: int
    integrity: str


def _log_migration_notice(_connection: object, message: object) -> None:
    """Forward PostgreSQL migration notices to the service log."""
    logger.info("PostgreSQL migration notice: %s", message)


async def _apply_migration_sql(
    conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
    cleaned_sql: str,
    version: int,
    sha256: str,
) -> None:
    """Execute one migration and record it, forwarding server notices."""
    forwards_notices = isinstance(
        conn,
        asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
    )
    if forwards_notices:
        conn.add_log_listener(_log_migration_notice)
    try:
        async with conn.transaction():
            await conn.execute(cleaned_sql)
            has_hash_column = bool(
                await conn.fetchval(
                    "SELECT EXISTS("
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_schema = current_schema() "
                    "AND table_name = 'schema_migrations' AND column_name = 'sha256'"
                    ")"
                )
            )
            if has_hash_column:
                await conn.execute(
                    "INSERT INTO schema_migrations (version, sha256) VALUES ($1, $2)",
                    version,
                    sha256,
                )
            else:
                await conn.execute("INSERT INTO schema_migrations (version) VALUES ($1)", version)
    finally:
        if forwards_notices:
            conn.remove_log_listener(_log_migration_notice)


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


def _migrations_dir() -> Path:
    """Resolve the packaged migration directory for containers and local development."""
    container = Path("/app/db/migrations")
    if container.exists():
        return container
    return Path(__file__).resolve().parents[3] / "db" / "migrations"


def _migration_manifest_path() -> Path:
    """Resolve the packaged migration-integrity manifest."""
    container = Path("/app/db/ownership-manifest.json")
    if container.exists():
        return container
    return Path(__file__).resolve().parents[3] / "db" / "ownership-manifest.json"


def _migration_files(directory: Path) -> list[tuple[int, Path]]:
    """Return migration files ordered by revision, rejecting duplicate revisions."""
    files: list[tuple[int, Path]] = []
    seen_versions: dict[int, str] = {}
    for sql_file in sorted(directory.glob("*.sql")):
        try:
            version = int(sql_file.name.split("_", maxsplit=1)[0])
        except ValueError:
            logger.warning("Skipping non-migration file: %s", sql_file.name)
            continue
        if version in seen_versions:
            raise RuntimeError(
                f"duplicate migration version: {version} "
                f"({sql_file.name} vs {seen_versions[version]})"
            )
        seen_versions[version] = sql_file.name
        files.append((version, sql_file))
    return files


def _verified_migration_hashes() -> dict[int, str]:
    """Validate every packaged migration file against the committed manifest.

    This check happens before any migration DDL.  It detects a changed applied
    file as well as an untracked new file, so neither can be silently accepted.
    """
    try:
        manifest = json.loads(_migration_manifest_path().read_text(encoding="utf-8"))
        baseline = manifest["compatibility_baseline"]
        unhashed = baseline["unhashed_revisions"]
        entries = baseline["retained_migrations"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RuntimeError("migration integrity manifest is unavailable or invalid") from exc

    if unhashed != {"first": 1, "last": 101, "marker": "squashed_baseline_source_unavailable"}:
        raise RuntimeError("migration integrity manifest has an invalid squashed baseline marker")
    if not isinstance(entries, list):
        raise RuntimeError("migration integrity manifest has invalid retained migrations")

    expected: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise RuntimeError("migration integrity manifest has an invalid migration entry")
        path = entry.get("path")
        sha256 = entry.get("sha256")
        if not isinstance(path, str) or not isinstance(sha256, str):
            raise RuntimeError("migration integrity manifest has an incomplete migration entry")
        expected[path] = sha256

    files = _migration_files(_migrations_dir())
    # The manifest deliberately stores portable filenames rather than local
    # checkout-relative paths.
    actual_paths = {path.name for _version, path in files}
    if actual_paths != set(expected):
        raise RuntimeError("packaged migration files do not match the integrity manifest")

    hashes: dict[int, str] = {}
    for version, path in files:
        actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        expected_hash = expected[path.name]
        if actual_hash != expected_hash:
            raise RuntimeError(f"migration integrity mismatch for revision {version}")
        hashes[version] = actual_hash
    return hashes


def required_code_schema() -> int:
    """Return the minimum schema version this build requires to run.

    Reads ``db/SCHEMA_VERSION`` — the highest migration version this build
    ships, bumped by every migration that lands rather than only at a squash.
    The floor is asserted after the apply loop, so an older database is carried
    up to it instead of being refused; only one that is still below it when
    there is nothing left to apply fails to start.

    Falls back to a module constant when the file is absent, silently, because
    not every image ships it — an absent file is expected, not a
    misconfiguration. A file that is present but unparseable does indicate a
    packaging fault, so that case warns before degrading to the same constant.
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


async def _wait_for_schema_floor_after_contention(
    conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
) -> None:
    """Wait briefly for the lock holder to bring the database to this build's floor.

    A plain floor assertion would reject a compatible second instance while
    the first instance is still applying migrations. The wait remains bounded
    so a stalled migrator cannot make startup serve an unverified schema.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _SCHEMA_FLOOR_CONTENTION_TIMEOUT_SECONDS
    while True:
        try:
            await _assert_schema_floor(conn)
            return
        except RuntimeError:
            delay = min(
                _SCHEMA_FLOOR_CONTENTION_POLL_SECONDS,
                deadline - loop.time(),
            )
            if delay <= 0:
                raise
            await asyncio.sleep(delay)


async def check_migrations(pool: asyncpg.Pool) -> MigrationCheck:
    """Verify packaged migration integrity and the live schema without writing.

    Runtime services call this before hooks that can write or start workers.
    The query path is deliberately limited to ``SELECT`` statements: migration
    metadata is created and repaired only by :func:`run_migrations`.

    Parameters
    ----------
    pool:
        Runtime-role asyncpg pool connected to the JARVIS database.

    Returns
    -------
    MigrationCheck
        Current role and matching packaged/live revision details.

    Raises
    ------
    RuntimeError
        If a revision is missing, below the packaged floor, or has a mismatched
        recorded hash.
    """
    expected_hashes = _verified_migration_hashes()
    expected_versions = set(range(1, 102)) | set(expected_hashes)
    floor = required_code_schema()

    async with pool.acquire() as conn:
        current_user = str(await conn.fetchval("SELECT current_user"))
        rows = await conn.fetch("SELECT version, sha256 FROM ops.schema_migrations")

    applied = {int(row["version"]): row["sha256"] for row in rows}
    missing = expected_versions - set(applied)
    if missing:
        raise RuntimeError(f"database schema is missing migration revisions: {sorted(missing)}")

    for version, expected_hash in expected_hashes.items():
        if applied[version] != expected_hash:
            raise RuntimeError(f"database migration hash mismatch for revision {version}")

    live_version = max(applied, default=0)
    if live_version < floor:
        raise RuntimeError(
            f"refusing to start: database schema is at version {live_version}, but this "
            f"build requires at least {floor}. Apply the missing migrations or restore "
            "from a compatible backup."
        )
    return MigrationCheck(
        current_user=current_user,
        packaged_version=floor,
        live_version=live_version,
        integrity="ok",
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
    """Apply unapplied SQL migrations as the dedicated migration authority.

    Holds a Postgres advisory transaction lock (key 42) so concurrent service
    replicas do not race.  Each migration file is applied inside a savepoint;
    ``BEGIN``/``COMMIT``/``ROLLBACK`` lines at the outer transaction level are
    stripped to avoid conflicts with asyncpg's own transaction wrapping.

    Parameters
    ----------
    pool:
        asyncpg connection pool to run migrations against.
    migrations_dir:
        Directory containing ``NNN_*.sql`` migration files. When ``None``,
        uses the packaged directory after validating it against the integrity
        manifest. Explicit directories are reserved for isolated test fixtures.

    Raises
    ------
    RuntimeError
        If another instance holds the migration lock and
        ``JARVIS_MIGRATION_LOCK_CONTENDED_OK`` is not set, or if a duplicate
        migration version number is detected in the directory.

    """
    packaged_hashes = _verified_migration_hashes()
    migration_dir = migrations_dir or _migrations_dir()
    if not migration_dir.exists():
        raise RuntimeError(f"migrations directory not found: {migration_dir}")
    migration_files = _migration_files(migration_dir)

    async with pool.acquire() as conn:
        async with conn.transaction():
            # Fresh installs place metadata in ops, while pre-0114 upgrades
            # still find the legacy public table through this fallback.
            await conn.execute("SET LOCAL search_path = ops, public")
            # Bound the advisory-lock wait so a crashed holder never stalls startup.
            await conn.execute("SET LOCAL lock_timeout = '60s'")
            try:
                # Keep a lock-timeout error inside a savepoint so the outer
                # transaction remains usable for the compatibility recheck.
                async with conn.transaction():
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
                    logger.warning("%s; waiting briefly for schema compatibility", message)
                    await _wait_for_schema_floor_after_contention(conn)
                    return
                raise RuntimeError(f"{message}; refusing to start with unverified schema") from None
            await _repair_false_applied_migrations(conn)
            applied = {
                r["version"] for r in await conn.fetch("SELECT version FROM schema_migrations")
            }
            for version, sql_file in migration_files:
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
                sha256 = packaged_hashes.get(version, hashlib.sha256(sql.encode()).hexdigest())
                await _apply_migration_sql(conn, cleaned_sql, version, sha256)
                logger.info("Migration %s applied successfully", version)

            # Still inside the advisory-locked transaction: refuse to serve on a
            # schema below the baseline this build was packaged with.
            await _assert_schema_floor(conn)
