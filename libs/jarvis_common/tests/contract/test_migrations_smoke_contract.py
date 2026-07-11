"""Predicate-direct contract smoke tests for run_migrations (A269).

The session-scoped _contract_pool fixture has already called run_migrations()
once at session start (applying db/init.sql + all migrations). These tests
exercise idempotence — a second call to run_migrations() must complete without
error and must not insert additional schema_migrations rows.

Verified: libs/jarvis_common/jarvis_common/migrations.py:125-235 at HEAD.
Survivor-of an earlier consolidation: migration unit tests that mocked pool.acquire() replaced
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


async def test_0102_webauthn_schema_applied(_contract_pool):
    """0102 created the passkey tables + sessions.credential_id and lifted the floor to 102.

    The session fixture applies db/init.sql (baseline 1..101) then run_migrations(),
    which globs db/migrations/0102_webauthn_credentials.sql. This proves the DDL
    actually PREPARES and executes on a real pg16.8 — a mock cannot catch a bad
    column type or a malformed foreign key.

    Verified: db/migrations/0102_webauthn_credentials.sql; db/SCHEMA_VERSION == 102.
    """
    async with _contract_pool.acquire() as conn:
        tables = {
            r["table_name"]
            for r in await conn.fetch(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' "
                "AND table_name IN ('webauthn_credentials', 'webauthn_challenges')"
            )
        }
        assert tables == {"webauthn_credentials", "webauthn_challenges"}, (
            f"0102 did not create both passkey tables; found {tables}"
        )

        credential_col = await conn.fetchval(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'sessions' "
            "AND column_name = 'credential_id'"
        )
        assert credential_col == "uuid", (
            f"sessions.credential_id missing or wrong type: {credential_col!r}"
        )

        max_version = await conn.fetchval("SELECT max(version) FROM schema_migrations")
        assert max_version >= 102, f"schema floor not lifted to 102; max applied = {max_version}"
