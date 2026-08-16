"""Historical schema-101 migration contract against real PostgreSQL."""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

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


def _latest_migration_version(migrations_dir: Path = _MIGRATIONS) -> int:
    versions = [
        int(match.group(1))
        for path in migrations_dir.glob("*.sql")
        if (match := re.match(r"^(\d+)_", path.name)) is not None
    ]
    assert versions, "migration directory contains no numbered SQL files"
    return max(versions)


async def _project_collection_column(
    conn: asyncpg.Connection,
) -> tuple[str, str] | None:
    """Return the project Zotero collection column's type and nullability."""
    row = await conn.fetchrow(
        """
        SELECT data_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'projects'
          AND column_name = 'zotero_collection_key'
        """
    )
    if row is None:
        return None
    return str(row["data_type"]), str(row["is_nullable"])


async def test_schema_101_fixture_migrates_through_legacy_chain(
    live_pg_dsn: str, tmp_path: Path
) -> None:
    """The schema-101 origin survives the retained pre-ownership migrations."""
    # Verified: libs/jarvis_common/jarvis_common/migrations.py:247
    legacy_migrations = tmp_path / "migrations"
    legacy_migrations.mkdir()
    for migration in _MIGRATIONS.glob("*.sql"):
        if int(migration.name.split("_", maxsplit=1)[0]) <= 113:
            (legacy_migrations / migration.name).symlink_to(migration)

    pool = await asyncpg.create_pool(
        live_pg_dsn,
        min_size=1,
        max_size=2,
        init=init_pg_connection,
    )
    try:
        async with pool.acquire() as conn:
            await conn.execute(_SEED.read_text(encoding="utf-8"))

        with patch("jarvis_common.migrations.required_code_schema", return_value=113):
            await run_migrations(pool, migrations_dir=legacy_migrations)

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
            project_collection_column = await _project_collection_column(conn)

            assert version == _latest_migration_version(legacy_migrations)
            assert webauthn_table == "webauthn_credentials"
            assert visibility_scope == "private"
            assert content_generation == 0
            assert owner_user_id == 1
            assert invalid_pairing_count == 0
            assert external_id == "local:" + "a" * 64
            assert owner_constraint_count == 1
            assert project_collection_column == ("text", "YES")

        # The restore round trip rebuilds this historical origin after first
        # exercising the current schema. Prove the seed removes post-101
        # objects before the migration chain is applied a second time.
        async with pool.acquire() as conn:
            await conn.execute(_SEED.read_text(encoding="utf-8"))
            focus_table_before = await conn.fetchval("SELECT to_regclass('public.focus_sessions')")
            assert focus_table_before is None

        with patch("jarvis_common.migrations.required_code_schema", return_value=113):
            await run_migrations(pool, migrations_dir=legacy_migrations)

        async with pool.acquire() as conn:
            version = await conn.fetchval("SELECT max(version) FROM schema_migrations")
            focus_table_after = await conn.fetchval("SELECT to_regclass('public.focus_sessions')")
            project_collection_column = await _project_collection_column(conn)
            assert version == _latest_migration_version(legacy_migrations)
            assert focus_table_after == "focus_sessions"
            assert project_collection_column == ("text", "YES")
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
