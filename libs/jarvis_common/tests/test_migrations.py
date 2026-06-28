"""Tests for the advisory-lock error handling in run_migrations.

Covers the two paths in the try/except block around pg_advisory_xact_lock:
  - LockNotAvailableError (sqlstate 55P03) → swallowed (or raises RuntimeError
    when the compat env var is unset).
  - A generic PostgresError with a different sqlstate → re-raised as-is.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock

import asyncpg
import pytest
from jarvis_common.migrations import required_code_schema, run_migrations
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
            None,  # SET LOCAL lock_timeout = '60s'
            _make_lock_not_available(),  # SELECT pg_advisory_xact_lock(42)
        ]
    )

    with pytest.raises(RuntimeError, match="migration lock contended"):
        await run_migrations(pool, migrations_dir=tmp_path)


@pytest.mark.asyncio
async def test_lock_not_available_swallowed_with_compat_flag(tmp_path, monkeypatch) -> None:
    """LockNotAvailableError (sqlstate 55P03) → silently skipped when compat flag is set."""
    monkeypatch.setenv("JARVIS_MIGRATION_LOCK_CONTENDED_OK", "true")

    pool, _conn = _pool_with_execute_effects(
        [
            None,  # SET LOCAL lock_timeout = '60s'
            _make_lock_not_available(),  # SELECT pg_advisory_xact_lock(42)
        ]
    )

    # Must not raise
    await run_migrations(pool, migrations_dir=tmp_path)


@pytest.mark.asyncio
async def test_generic_postgres_error_is_reraised(tmp_path, monkeypatch) -> None:
    """A PostgresError with a non-55P03 sqlstate must propagate unchanged."""
    monkeypatch.delenv("JARVIS_MIGRATION_LOCK_CONTENDED_OK", raising=False)

    connection_error = _make_generic_postgres_error("08006")
    pool, _conn = _pool_with_execute_effects(
        [
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


def test_code_max_migration_returns_floor_on_empty_dir(tmp_path, monkeypatch) -> None:
    """An empty/absent migrations dir reports the baseline floor, not None.

    Keeps restore-point compatibility armed after the schema squash emptied
    ``db/migrations/`` — a missing dir must not degrade compat to "unknown".
    """
    monkeypatch.setenv("DB_MIGRATIONS_DIR", str(tmp_path))  # exists but empty
    from paper_ingestion.routers.backups import _code_max_migration  # noqa: PLC0415

    assert _code_max_migration() == required_code_schema()


def test_required_code_schema_reads_schema_version_file() -> None:
    """required_code_schema() reads db/SCHEMA_VERSION, the single source of truth."""
    assert required_code_schema() == 101


def test_required_code_schema_absent_file_is_silent(tmp_path, monkeypatch, caplog) -> None:
    """An absent SCHEMA_VERSION file (not shipped into this image) degrades to the
    built-in floor WITHOUT a warning — it must not cry wolf on every boot."""
    monkeypatch.setattr(
        "jarvis_common.migrations._schema_version_path",
        lambda: tmp_path / "missing" / "SCHEMA_VERSION",
    )

    with caplog.at_level(logging.WARNING):
        assert required_code_schema() == 101

    assert not any("could not read" in r.getMessage() for r in caplog.records)


def test_required_code_schema_warns_on_corrupt_file(tmp_path, monkeypatch, caplog) -> None:
    """A present-but-unparseable SCHEMA_VERSION file degrades to the floor AND warns —
    that genuinely indicates a packaging problem, unlike an absent file."""
    bad = tmp_path / "SCHEMA_VERSION"
    bad.write_text("not-a-number\n")
    monkeypatch.setattr("jarvis_common.migrations._schema_version_path", lambda: bad)

    with caplog.at_level(logging.WARNING):
        assert required_code_schema() == 101

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
