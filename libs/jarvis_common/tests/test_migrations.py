"""Tests for the advisory-lock error handling in run_migrations.

Covers the two paths in the try/except block around pg_advisory_xact_lock:
  - LockNotAvailableError (sqlstate 55P03) → swallowed (or raises RuntimeError
    when the compat env var is unset).
  - A generic PostgresError with a different sqlstate → re-raised as-is.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import asyncpg
import pytest
from jarvis_common.migrations import (
    _REQUIRED_CODE_SCHEMA_FALLBACK,
    check_migrations,
    required_code_schema,
    run_migrations,
)
from jarvis_common.testing_db import make_pool_and_conn

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_lock_not_available() -> asyncpg.LockNotAvailableError:
    """Construct an asyncpg.LockNotAvailableError (sqlstate 55P03)."""
    exc = asyncpg.LockNotAvailableError()
    # The class already carries sqlstate as a class attribute — verify it.
    assert getattr(exc, "sqlstate", None) == "55P03"
    return exc


def _make_generic_postgres_error(sqlstate: str = "08006") -> asyncpg.PostgresError:
    """Construct a plain asyncpg.PostgresError with an arbitrary sqlstate."""
    exc = asyncpg.PostgresError()
    # asyncpg errors are typically created by the C layer; set sqlstate on the
    # instance directly to simulate a real wire error with a non-lock sqlstate.
    exc.sqlstate = sqlstate  # type: ignore[attr-defined]
    return exc


def _pool_with_execute_effects(effects: list):
    """Return (pool, conn) where conn.execute raises/returns per *effects*."""
    pool, conn = make_pool_and_conn(fetch_return=[])
    conn.execute = AsyncMock(side_effect=effects)
    return pool, conn


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lock_not_available_raises_runtime_error_without_compat_flag(
    tmp_path, monkeypatch
) -> None:
    """LockNotAvailableError (sqlstate 55P03) → RuntimeError when compat flag absent."""
    monkeypatch.delenv("JARVIS_MIGRATION_LOCK_CONTENDED_OK", raising=False)

    pool, _conn = _pool_with_execute_effects(
        [
            None,  # SET LOCAL search_path = ops, public
            None,  # SET LOCAL lock_timeout = '60s'
            _make_lock_not_available(),  # SELECT pg_advisory_xact_lock(42)
        ]
    )

    with pytest.raises(RuntimeError, match="migration lock contended"):
        await run_migrations(pool, migrations_dir=tmp_path)


@pytest.mark.asyncio
async def test_lock_contention_waits_until_the_schema_reaches_the_floor(
    tmp_path, monkeypatch
) -> None:
    """The compatibility path starts after the other migrator reaches the floor."""
    monkeypatch.setenv("JARVIS_MIGRATION_LOCK_CONTENDED_OK", "true")
    floor = required_code_schema()

    pool, conn = _pool_with_execute_effects(
        [
            None,  # SET LOCAL search_path = ops, public
            None,  # SET LOCAL lock_timeout = '60s'
            _make_lock_not_available(),  # SELECT pg_advisory_xact_lock(42)
        ]
    )
    conn.fetchval = AsyncMock(side_effect=[floor - 1, floor])
    sleep = AsyncMock()
    monkeypatch.setattr("jarvis_common.migrations.asyncio.sleep", sleep)

    await run_migrations(pool, migrations_dir=tmp_path)

    assert conn.fetchval.await_count == 2
    sleep.assert_awaited_once()


@pytest.mark.asyncio
async def test_lock_contention_refuses_a_schema_still_below_the_floor(
    tmp_path, monkeypatch
) -> None:
    """The compatibility path fails closed when its bounded wait expires."""
    monkeypatch.setenv("JARVIS_MIGRATION_LOCK_CONTENDED_OK", "true")
    monkeypatch.setattr(
        "jarvis_common.migrations._SCHEMA_FLOOR_CONTENTION_TIMEOUT_SECONDS",
        0.0,
    )
    floor = required_code_schema()
    pool, conn = _pool_with_execute_effects(
        [
            None,  # SET LOCAL search_path = ops, public
            None,  # SET LOCAL lock_timeout = '60s'
            _make_lock_not_available(),
        ]
    )
    conn.fetchval = AsyncMock(return_value=floor - 1)

    with pytest.raises(RuntimeError, match="refusing to start"):
        await run_migrations(pool, migrations_dir=tmp_path)

    conn.fetchval.assert_awaited_once()


@pytest.mark.asyncio
async def test_generic_postgres_error_is_reraised(tmp_path, monkeypatch) -> None:
    """A PostgresError with a non-55P03 sqlstate must propagate unchanged."""
    monkeypatch.delenv("JARVIS_MIGRATION_LOCK_CONTENDED_OK", raising=False)

    connection_error = _make_generic_postgres_error("08006")
    pool, _conn = _pool_with_execute_effects(
        [
            None,  # SET LOCAL search_path = ops, public
            None,  # SET LOCAL lock_timeout = '60s'
            connection_error,  # SELECT pg_advisory_xact_lock(42)
        ]
    )

    with pytest.raises(asyncpg.PostgresError) as exc_info:
        await run_migrations(pool, migrations_dir=tmp_path)

    # Must be the exact same exception object, not wrapped
    assert exc_info.value is connection_error


# ---------------------------------------------------------------------------
# Schema floor — refuse to start on an under-baseline database
# ---------------------------------------------------------------------------


def _pool_at_schema(max_applied: int):
    """Pool/conn mocks whose advisory lock succeeds and whose
    ``MAX(version)`` query reports *max_applied* — to exercise the floor.
    """
    pool, _conn = make_pool_and_conn(fetch_return=[], fetchval_return=max_applied)
    return pool


@pytest.mark.asyncio
async def test_under_baseline_schema_refuses_to_start(tmp_path, monkeypatch) -> None:
    """A DB whose MAX(version) is below the required floor → RuntimeError."""
    monkeypatch.delenv("JARVIS_MIGRATION_LOCK_CONTENDED_OK", raising=False)
    floor = required_code_schema()

    pool = _pool_at_schema(floor - 1)

    with pytest.raises(RuntimeError, match="refusing to start"):
        await run_migrations(pool, migrations_dir=tmp_path)


@pytest.mark.asyncio
async def test_baseline_schema_passes_the_floor(tmp_path, monkeypatch) -> None:
    """A DB premarked exactly to the required floor must not raise."""
    monkeypatch.delenv("JARVIS_MIGRATION_LOCK_CONTENDED_OK", raising=False)
    floor = required_code_schema()

    pool = _pool_at_schema(floor)

    await run_migrations(pool, migrations_dir=tmp_path)  # must not raise


@pytest.mark.asyncio
async def test_absent_migrations_dir_still_refuses_under_baseline_schema(
    tmp_path, monkeypatch
) -> None:
    """A dedicated migrator refuses an unavailable migration package."""
    monkeypatch.delenv("JARVIS_MIGRATION_LOCK_CONTENDED_OK", raising=False)
    floor = required_code_schema()
    absent_dir = tmp_path / "no-such-migrations"
    assert not absent_dir.exists()

    pool = _pool_at_schema(floor - 1)

    with pytest.raises(RuntimeError, match="migrations directory not found"):
        await run_migrations(pool, migrations_dir=absent_dir)


@pytest.mark.asyncio
async def test_absent_migrations_dir_starts_when_floor_is_satisfied(tmp_path, monkeypatch) -> None:
    """An at-floor database cannot hide an unavailable migration package."""
    monkeypatch.delenv("JARVIS_MIGRATION_LOCK_CONTENDED_OK", raising=False)
    floor = required_code_schema()
    absent_dir = tmp_path / "no-such-migrations"
    assert not absent_dir.exists()

    pool = _pool_at_schema(floor)

    with pytest.raises(RuntimeError, match="migrations directory not found"):
        await run_migrations(pool, migrations_dir=absent_dir)


@pytest.mark.asyncio
async def test_runtime_check_is_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """The runtime schema check neither applies DDL nor repairs metadata."""
    expected_hash = "a" * 64
    monkeypatch.setattr(
        "jarvis_common.migrations._verified_migration_hashes", lambda: {102: expected_hash}
    )
    monkeypatch.setattr("jarvis_common.migrations.required_code_schema", lambda: 102)
    rows = [{"version": version, "sha256": None} for version in range(1, 102)]
    rows.append({"version": 102, "sha256": expected_hash})
    pool, conn = make_pool_and_conn(fetch_return=rows, fetchval_return="jarvis_research_runtime")

    result = await check_migrations(pool)

    assert result.current_user == "jarvis_research_runtime"
    assert result.integrity == "ok"
    conn.execute.assert_not_awaited()


def test_changed_packaged_migration_fails_before_database_access(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A changed applied file is rejected before a new migration can execute."""
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    migration = migrations_dir / "0102_example.sql"
    migration.write_text("SELECT 1;\n", encoding="utf-8")
    manifest = {
        "compatibility_baseline": {
            "unhashed_revisions": {
                "first": 1,
                "last": 101,
                "marker": "squashed_baseline_source_unavailable",
            },
            "retained_migrations": [{"path": "0102_example.sql", "sha256": "0" * 64}],
        }
    }
    manifest_path = tmp_path / "ownership-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr("jarvis_common.migrations._migrations_dir", lambda: migrations_dir)
    monkeypatch.setattr("jarvis_common.migrations._migration_manifest_path", lambda: manifest_path)

    with pytest.raises(RuntimeError, match="migration integrity mismatch"):
        from jarvis_common.migrations import _verified_migration_hashes

        _verified_migration_hashes()


def test_code_max_migration_returns_floor_on_empty_dir(tmp_path, monkeypatch) -> None:
    """An empty/absent migrations dir reports the baseline floor, not None.

    Keeps restore-point compatibility armed after the schema squash emptied
    ``db/migrations/`` — a missing dir must not degrade compat to "unknown".
    """
    monkeypatch.setenv("DB_MIGRATIONS_DIR", str(tmp_path))  # exists but empty
    from paper_ingestion.services.backup_archive import _code_max_migration  # noqa: PLC0415

    assert _code_max_migration() == required_code_schema()


def test_required_code_schema_reads_schema_version_file() -> None:
    """required_code_schema() reads db/SCHEMA_VERSION, the single source of truth."""
    from pathlib import Path  # noqa: PLC0415

    from jarvis_common import migrations as _m  # noqa: PLC0415

    repo_root = Path(_m.__file__).resolve().parents[3]
    expected = int((repo_root / "db" / "SCHEMA_VERSION").read_text().strip())
    assert required_code_schema() == expected


def test_packaged_migration_hashes_match_the_tracked_files() -> None:
    """Every retained migration must still match its recorded immutable hash."""
    from jarvis_common.migrations import _verified_migration_hashes

    hashes = _verified_migration_hashes()

    assert hashes[114] == "d05ecc9f04e1fed68246e958d3afbff3a8d72bda522095c75e941aac13b59374"


def test_required_code_schema_absent_file_is_silent(tmp_path, monkeypatch, caplog) -> None:
    """An absent SCHEMA_VERSION file (not shipped into this image) degrades to the
    built-in floor WITHOUT a warning — it must not cry wolf on every boot."""
    monkeypatch.setattr(
        "jarvis_common.migrations._schema_version_path",
        lambda: tmp_path / "missing" / "SCHEMA_VERSION",
    )

    with caplog.at_level(logging.WARNING):
        assert required_code_schema() == _REQUIRED_CODE_SCHEMA_FALLBACK

    assert not any("could not read" in r.getMessage() for r in caplog.records)


def test_required_code_schema_warns_on_corrupt_file(tmp_path, monkeypatch, caplog) -> None:
    """A present-but-unparseable SCHEMA_VERSION file degrades to the floor AND warns —
    that genuinely indicates a packaging problem, unlike an absent file."""
    bad = tmp_path / "SCHEMA_VERSION"
    bad.write_text("not-a-number\n")
    monkeypatch.setattr("jarvis_common.migrations._schema_version_path", lambda: bad)

    with caplog.at_level(logging.WARNING):
        assert required_code_schema() == _REQUIRED_CODE_SCHEMA_FALLBACK

    assert any("could not read" in r.getMessage() for r in caplog.records)


def test_schema_version_file_matches_fallback_constant() -> None:
    """db/SCHEMA_VERSION and the in-code fallback must agree so the floor has one
    value regardless of which is read (the file on the host / in images that ship it;
    the constant elsewhere). This guard closes the bump-the-file-forget-the-constant trap."""
    from pathlib import Path  # noqa: PLC0415

    from jarvis_common import migrations as _m  # noqa: PLC0415

    repo_root = Path(_m.__file__).resolve().parents[3]
    file_value = int((repo_root / "db" / "SCHEMA_VERSION").read_text().strip())
    assert file_value == _m._REQUIRED_CODE_SCHEMA_FALLBACK


# ---------------------------------------------------------------------------
# Migration file hygiene and replay safety
# ---------------------------------------------------------------------------

# A complete ``SET search_path ...;`` statement.  The ``SET search_path`` that
# follows a ``CREATE FUNCTION`` header is an attribute clause, not a statement:
# it is never terminated on its own line and it rejects ``LOCAL``.
_STATEMENT_SEARCH_PATH_RE = re.compile(
    r"^SET\s+(?P<local>LOCAL\s+)?search_path\b[^;]*;\s*$", re.IGNORECASE
)

_REPLAYED_MIGRATIONS = (
    "0115_cross_domain_boundaries.sql",
    "0116_unified_job_facade.sql",
    "0117_owner_capabilities.sql",
)

_REPLAYED_INDEXES = frozenset(
    {
        "research_domain_events_pending_idx",
        "research_domain_events_active_deletion_idx",
        "learning_domain_commands_pending_idx",
        "platform_erasure_requests_one_active_user_idx",
        "audit_log_subject_id_idx",
        "config_deliveries_due_idx",
    }
)

# ``CREATE TABLE IF NOT EXISTS`` accepts a table whose columns differ, so a
# half-applied revision would replay cleanly and only fail later, in the grants
# that assume these columns.  Replay must leave the declared shape intact.
_DECLARED_COLUMNS: dict[tuple[str, str], frozenset[str]] = {
    ("research", "domain_events"): frozenset(
        {
            "id",
            "event_type",
            "user_id",
            "paper_id",
            "payload",
            "attempts",
            "next_attempt_at",
            "last_error",
            "delivered_at",
            "dead_lettered_at",
            "created_at",
        }
    ),
    ("research", "pending_paper_deletions"): frozenset(
        {"event_id", "user_id", "paper_id", "created_at"}
    ),
    ("research", "zotero_push_claims"): frozenset(
        {"paper_id", "user_id", "lease_id", "lease_expires_at"}
    ),
    ("learning", "domain_commands"): frozenset(
        {
            "id",
            "command_type",
            "request_id",
            "user_id",
            "paper_id",
            "payload",
            "received_at",
            "processed_at",
            "acknowledgement_at",
            "last_error",
        }
    ),
    ("platform", "erasure_requests"): frozenset(
        {
            "request_id",
            "user_id",
            "state",
            "resume_state",
            "attempts",
            "next_attempt_at",
            "last_error",
            "requested_at",
            "eligible_at",
            "completed_at",
        }
    ),
    ("platform", "erasure_acknowledgements"): frozenset(
        {"request_id", "domain", "receipt", "acknowledged_at"}
    ),
    ("platform", "audit_subjects"): frozenset(
        {"id", "user_id", "metadata", "created_at", "updated_at"}
    ),
    ("platform", "config_deliveries"): frozenset(
        {
            "scope_user_id",
            "actor_user_id",
            "key",
            "delivery_id",
            "user_role",
            "session_id",
            "zotero_scope_changed",
            "state",
            "attempts",
            "next_attempt_at",
            "last_error",
            "updated_at",
        }
    ),
    ("ops", "job_owner_registry"): frozenset({"task_name", "queue_name", "service_name"}),
}

_OWNED_SCHEMAS = ("research", "learning", "platform", "ops")


def _db_dir() -> Path:
    """Resolve the tracked db/ directory from this test file."""
    return Path(__file__).resolve().parents[3] / "db"


def test_no_migration_sets_a_session_scoped_search_path() -> None:
    """A statement-position ``SET search_path`` must be written ``SET LOCAL``.

    ``run_migrations`` applies every file inside one transaction, so a
    session-scoped statement outlives the migration that ran it and leaves the
    connection on a search path nothing else chose.
    """
    migration_files = sorted((_db_dir() / "migrations").glob("*.sql"))
    assert migration_files, "no migration files were scanned"

    session_scoped = [
        f"{path.name}:{number}"
        for path in migration_files
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if (match := _STATEMENT_SEARCH_PATH_RE.match(line)) and not match.group("local")
    ]
    assert session_scoped == []


async def _owned_schema_columns(
    conn: asyncpg.Connection,
) -> dict[tuple[str, str], frozenset[str]]:
    """Return the column names of every table in the owned schemas."""
    shape: dict[tuple[str, str], set[str]] = {}
    for row in await conn.fetch(
        """
        SELECT table_schema, table_name, column_name
        FROM information_schema.columns
        WHERE table_schema = ANY($1::text[])
        """,
        list(_OWNED_SCHEMAS),
    ):
        shape.setdefault((row["table_schema"], row["table_name"]), set()).add(row["column_name"])
    return {table: frozenset(columns) for table, columns in shape.items()}


def _assert_declared_columns(shape: dict[tuple[str, str], frozenset[str]]) -> None:
    """Fail when a replayed migration's table lost a column it declares."""
    for table, declared in _DECLARED_COLUMNS.items():
        qualified = ".".join(table)
        assert table in shape, f"{qualified} does not exist"
        assert declared <= shape[table], (
            f"{qualified} is missing declared columns: {sorted(declared - shape[table])}"
        )


async def _insert_job(conn: asyncpg.Connection, task_name: str, status: str, job_id: str) -> None:
    """Insert one research-owned job carrying a public job id.

    The replay fixture connects with a bare ``asyncpg.connect``, which registers
    no JSONB codec, so this argument really does have to arrive already encoded.
    """
    await conn.execute(  # nolint:jsonb-double-encode
        """
        INSERT INTO ops.procrastinate_jobs (queue_name, task_name, args, status)
        VALUES ('paper_ingestion', $1, $2::jsonb, $3::ops.procrastinate_job_status)
        """,
        task_name,
        json.dumps({"job_id": job_id, "user_id": 71}),
        status,
    )


@pytest.mark.contract
@pytest.mark.asyncio
async def test_replayed_migrations_converge_on_the_installed_baseline(live_pg_dsn: str) -> None:
    """Re-running 0115-0117 over an installed schema succeeds and changes nothing.

    Exercises the migration files themselves rather than ``db/init.sql``: the
    disposable database is built from ``db/init.sql`` and each file is then
    executed again, which is what a replay after a lost ``schema_migrations``
    marker does.  The contract suite cannot cover this — ``db/init.sql`` records
    every revision as applied, so the runner skips all of them there.
    """
    db_dir = _db_dir()
    conn = await asyncpg.connect(live_pg_dsn)
    try:
        await conn.execute((db_dir / "init.sql").read_text(encoding="utf-8"))
        installed_shape = await _owned_schema_columns(conn)

        for name in _REPLAYED_MIGRATIONS:
            async with conn.transaction():
                await conn.execute((db_dir / "migrations" / name).read_text(encoding="utf-8"))

        replayed_shape = await _owned_schema_columns(conn)
        assert replayed_shape == installed_shape
        _assert_declared_columns(replayed_shape)

        indexes = {
            row["indexname"]
            for row in await conn.fetch(
                "SELECT indexname FROM pg_indexes WHERE schemaname = ANY($1::text[])",
                list(_OWNED_SCHEMAS),
            )
        }
        assert _REPLAYED_INDEXES <= indexes
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM pg_constraint WHERE conname = 'audit_log_caller_role_check'"
            )
            == 1
        )

        # The replay installed 0116's job facade over db/init.sql's, so what
        # follows reads the migration route's registry seed and cancel function.
        test_job_id = str(uuid.uuid4())
        await _insert_job(conn, "noop.test", "todo", test_job_id)
        assert await conn.fetchrow(
            "SELECT owner_queue, owner_service FROM ops.procrastinate_jobs "
            "WHERE args->>'job_id' = $1",
            test_job_id,
        ) == ("paper_ingestion", "research")

        for status, cancelled in (("succeeded", False), ("aborting", False), ("doing", True)):
            job_id = str(uuid.uuid4())
            await _insert_job(conn, "paper.process", status, job_id)
            assert (
                await conn.fetchval("SELECT ops.jarvis_job_cancel_v1($1, '71')", job_id)
                is cancelled
            ), f"cancelling a {status} job reported the wrong result"
    finally:
        await conn.close()
