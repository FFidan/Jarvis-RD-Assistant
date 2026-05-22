"""Predicate-direct contract smoke tests for run_migrations (A269).

The session-scoped _contract_pool fixture has already called run_migrations()
once at session start (applying db/init.sql + all migrations). These tests
exercise idempotence — a second call to run_migrations() must complete without
error and must not insert additional schema_migrations rows.

Verified: libs/jarvis_common/jarvis_common/migrations.py:125-235 at HEAD.
Survivor-of (Phase C): migration unit tests that mocked pool.acquire() replaced
by this predicate-direct idempotence smoke.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]


async def test_a269_run_migrations_idempotent(_contract_pool):
    """A269: calling run_migrations() a second time is a no-op — no new schema_migrations rows.

    The session fixture already applied all migrations once. A second call
    with the same migrations_dir must leave the row count unchanged.

    Verified: migrations.py:193 — 'if version in applied: continue' skip path.
    """
    from jarvis_common.migrations import run_migrations

    db_dir = Path(__file__).resolve().parents[5] / "db"
    migrations_dir = db_dir / "migrations"

    # Count rows before second run
    async with _contract_pool.acquire() as conn:
        count_before = await conn.fetchval("SELECT COUNT(*) FROM schema_migrations")

    # Second run — must not raise
    await run_migrations(_contract_pool, migrations_dir=migrations_dir)

    async with _contract_pool.acquire() as conn:
        count_after = await conn.fetchval("SELECT COUNT(*) FROM schema_migrations")

    assert count_after == count_before, (
        f"run_migrations() inserted {count_after - count_before} unexpected row(s) "
        f"on second call (before={count_before}, after={count_after})"
    )


async def test_a269_run_migrations_no_exception_on_already_applied_schema(_contract_pool):
    """A269: run_migrations() completes without exception when all migrations are already applied.

    Smoke test: proves the function returns normally (no RuntimeError, no
    asyncpg.PostgresError) when the schema is fully up to date.

    Verified: migrations.py:125-235 — advisory lock acquired, all versions already
    in applied set, outer transaction commits cleanly.
    """
    from jarvis_common.migrations import run_migrations

    db_dir = Path(__file__).resolve().parents[5] / "db"
    migrations_dir = db_dir / "migrations"

    # Should not raise any exception
    await run_migrations(_contract_pool, migrations_dir=migrations_dir)
