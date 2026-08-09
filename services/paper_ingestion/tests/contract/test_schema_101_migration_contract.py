"""Historical schema-101 migration contract against real PostgreSQL."""

from __future__ import annotations

import re
from pathlib import Path

import asyncpg
import pytest

from jarvis_common.db_helpers import init_pg_connection
from jarvis_common.migrations import run_migrations

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]

_ROOT = Path(__file__).resolve().parents[4]
_MIGRATIONS = _ROOT / "db/migrations"
_SEED = _ROOT / "db/testdata/schema-101-seed.sql"


def _latest_migration_version() -> int:
    versions = [
        int(match.group(1))
        for path in _MIGRATIONS.glob("*.sql")
        if (match := re.match(r"^(\d+)_", path.name)) is not None
    ]
    assert versions, "migration directory contains no numbered SQL files"
    return max(versions)


async def test_schema_101_fixture_migrates_to_exact_current_contract(live_pg_dsn: str) -> None:
    """The actual lifecycle origin must survive every current migration."""
    # Verified: libs/jarvis_common/jarvis_common/migrations.py:247
    pool = await asyncpg.create_pool(
        live_pg_dsn,
        min_size=1,
        max_size=2,
        init=init_pg_connection,
    )
    try:
        async with pool.acquire() as conn:
            await conn.execute(_SEED.read_text(encoding="utf-8"))

        await run_migrations(pool, migrations_dir=_MIGRATIONS)

        async with pool.acquire() as conn:
            version = await conn.fetchval("SELECT max(version) FROM schema_migrations")
            webauthn_table = await conn.fetchval(
                "SELECT to_regclass('public.webauthn_credentials')"
            )
            visibility_scope = await conn.fetchval(
                "SELECT visibility_scope FROM papers WHERE id = 1"
            )
            content_generation = await conn.fetchval(
                "SELECT content_generation FROM papers WHERE id = 1"
            )
            owner_user_id = await conn.fetchval(
                "SELECT value FROM user_config WHERE key = 'owner.user_id' AND user_id IS NULL"
            )
            invalid_pairing_count = await conn.fetchval(
                "SELECT count(*) FROM telegram_user_pairings WHERE chat_id < 0"
            )
            external_id = await conn.fetchval("SELECT external_id FROM papers WHERE id = 1")
            owner_constraint_count = await conn.fetchval(
                "SELECT count(*) FROM pg_constraint "
                "WHERE conrelid = 'paper_contradictions'::regclass "
                "AND conname = 'chk_paper_contradictions_user_id_present'"
            )

            assert version == _latest_migration_version()
            assert webauthn_table == "webauthn_credentials"
            assert visibility_scope == "private"
            assert content_generation == 0
            assert owner_user_id == 1
            assert invalid_pairing_count == 0
            assert external_id == "local:" + "a" * 64
            assert owner_constraint_count == 1

        # The restore round trip rebuilds this historical origin after first
        # exercising the current schema. Prove the seed removes post-101
        # objects before the migration chain is applied a second time.
        async with pool.acquire() as conn:
            await conn.execute(_SEED.read_text(encoding="utf-8"))
            focus_table_before = await conn.fetchval("SELECT to_regclass('public.focus_sessions')")
            assert focus_table_before is None

        await run_migrations(pool, migrations_dir=_MIGRATIONS)

        async with pool.acquire() as conn:
            version = await conn.fetchval("SELECT max(version) FROM schema_migrations")
            focus_table_after = await conn.fetchval("SELECT to_regclass('public.focus_sessions')")
            assert version == _latest_migration_version()
            assert focus_table_after == "focus_sessions"
    finally:
        await pool.close()


async def test_schema_101_contract_rejects_missing_historical_column(live_pg_dsn: str) -> None:
    """Mutation proof: weakening the historical origin must break migration 0106."""
    # Verified: db/migrations/0106_paper_visibility_scope.sql:16
    pool = await asyncpg.create_pool(
        live_pg_dsn,
        min_size=1,
        max_size=2,
        init=init_pg_connection,
    )
    seed = _SEED.read_text(encoding="utf-8").replace(
        ",\n  discovery_origin text NOT NULL DEFAULT 'direct'\n",
        "\n",
    )
    try:
        async with pool.acquire() as conn:
            await conn.execute(seed)
        with pytest.raises(asyncpg.UndefinedColumnError, match="discovery_origin"):
            await run_migrations(pool, migrations_dir=_MIGRATIONS)
    finally:
        await pool.close()
