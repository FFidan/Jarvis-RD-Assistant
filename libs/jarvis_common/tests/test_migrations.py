"""Tests for the advisory-lock error handling in run_migrations.

Covers the two paths in the try/except block around pg_advisory_xact_lock:
  - LockNotAvailableError (sqlstate 55P03) → swallowed (or raises RuntimeError
    when the compat env var is unset).
  - A generic PostgresError with a different sqlstate → re-raised as-is.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import asyncpg
import pytest
from jarvis_common.migrations import run_migrations
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
