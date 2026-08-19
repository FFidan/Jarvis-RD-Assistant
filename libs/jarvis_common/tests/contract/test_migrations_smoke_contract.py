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

import uuid
from pathlib import Path

import asyncpg
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

    db_dir = Path(__file__).resolve().parents[4] / "db"
    migrations_dir = db_dir / "migrations"

    # run_migrations warns and returns on a missing dir, which would make this
    # test pass without applying anything.
    assert migrations_dir.is_dir(), f"migrations dir did not resolve: {migrations_dir}"

    # Count rows before second run
    async with _contract_pool.acquire() as conn:
        count_before = await conn.fetchval("SELECT COUNT(*) FROM ops.schema_migrations")

    assert count_before > 0, "no migrations were applied, so idempotence is untested"

    # Second run — must not raise
    await run_migrations(_contract_pool, migrations_dir=migrations_dir)

    async with _contract_pool.acquire() as conn:
        count_after = await conn.fetchval("SELECT COUNT(*) FROM ops.schema_migrations")

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

    db_dir = Path(__file__).resolve().parents[4] / "db"
    migrations_dir = db_dir / "migrations"

    # run_migrations warns and returns on a missing dir, which would make this
    # test pass without executing a single migration.
    assert migrations_dir.is_dir(), f"migrations dir did not resolve: {migrations_dir}"

    # Should not raise any exception
    await run_migrations(_contract_pool, migrations_dir=migrations_dir)


async def test_0114_upgrades_the_legacy_owner_through_the_migrator(live_pg_dsn: str) -> None:
    """0114 moves a version-113 database through the temporary legacy authority.

    Verified: db/migrations/0114_owned_schemas_and_roles.sql — temporary
    membership revocation and final ``ops.schema_migrations`` record.
    """
    db_dir = Path(__file__).resolve().parents[4] / "db"
    init_sql = (db_dir / "init.sql").read_text(encoding="utf-8")
    legacy_init, marker, _ = init_sql.partition("-- FRESH-INSTALL OWNERSHIP BOUNDARY")
    assert marker, "fresh ownership boundary marker is missing from db/init.sql"

    bootstrap = await asyncpg.connect(live_pg_dsn)
    try:
        await bootstrap.execute(legacy_init)
        await bootstrap.execute("DELETE FROM schema_migrations WHERE version >= 102")
        for migration in sorted((db_dir / "migrations").glob("01[0-1][0-9]_*.sql")):
            version = int(migration.name.split("_", maxsplit=1)[0])
            if version >= 114:
                continue
            await bootstrap.execute(migration.read_text(encoding="utf-8"))
            await bootstrap.execute("INSERT INTO schema_migrations (version) VALUES ($1)", version)
        await bootstrap.execute(
            "CREATE ROLE jarvis_bootstrap LOGIN PASSWORD 'bootstrap-contract-password' "
            "SUPERUSER CREATEROLE NOINHERIT NOBYPASSRLS; "
            "GRANT jarvis TO jarvis_bootstrap WITH ADMIN OPTION"
        )
        await bootstrap.execute(
            "CREATE ROLE jarvis_migrator LOGIN PASSWORD 'migration-contract-password' "
            "NOINHERIT NOBYPASSRLS"
        )
        await bootstrap.execute(
            "CREATE ROLE jarvis_erasure_executor LOGIN "
            "PASSWORD 'erasure-executor-contract-password' NOSUPERUSER NOCREATEDB "
            "NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS"
        )
        await bootstrap.execute(
            "CREATE ROLE jarvis_platform_owner NOLOGIN NOINHERIT NOBYPASSRLS; "
            "CREATE ROLE jarvis_research_owner NOLOGIN NOINHERIT NOBYPASSRLS; "
            "CREATE ROLE jarvis_learning_owner NOLOGIN NOINHERIT NOBYPASSRLS; "
            "CREATE ROLE jarvis_ops_owner NOLOGIN NOINHERIT NOBYPASSRLS"
        )
        await bootstrap.execute(
            "CREATE SCHEMA platform AUTHORIZATION jarvis_platform_owner; "
            "CREATE SCHEMA research AUTHORIZATION jarvis_research_owner; "
            "CREATE SCHEMA learning AUTHORIZATION jarvis_learning_owner; "
            "CREATE SCHEMA ops AUTHORIZATION jarvis_ops_owner"
        )
        await bootstrap.execute(
            "GRANT jarvis_platform_owner, jarvis_research_owner, jarvis_learning_owner, "
            "jarvis_ops_owner TO jarvis_bootstrap WITH ADMIN OPTION"
        )
        await bootstrap.close()
        bootstrap = await asyncpg.connect(
            live_pg_dsn,
            user="jarvis_bootstrap",
            password="bootstrap-contract-password",
        )
        # Renaming the historical database owner preserves the complete legacy
        # ownership graph, including the database and public schema.
        await bootstrap.execute("ALTER ROLE jarvis RENAME TO jarvis_legacy_rollback")
        await bootstrap.execute(
            "GRANT jarvis_platform_owner, jarvis_research_owner, jarvis_learning_owner, "
            "jarvis_ops_owner TO jarvis_legacy_rollback; "
            "GRANT jarvis_platform_owner, jarvis_research_owner, jarvis_learning_owner, "
            "jarvis_ops_owner TO jarvis_migrator WITH ADMIN OPTION"
        )
        await bootstrap.execute("GRANT SELECT, INSERT ON schema_migrations TO jarvis_migrator")
        await bootstrap.execute("GRANT jarvis_legacy_rollback TO jarvis_migrator WITH ADMIN OPTION")
        await bootstrap.execute(
            """
            INSERT INTO procrastinate_jobs (queue_name, task_name, args, status)
            VALUES
                ('paper_ingestion', 'paper.process', '{"job_id":"upgrade-known","user_id":7}'::jsonb, 'todo'),
                ('paper_ingestion', 'legacy.unknown', '{"job_id":"upgrade-history","user_id":7}'::jsonb, 'succeeded')
            """
        )

        migrator_pool = await asyncpg.create_pool(
            live_pg_dsn,
            user="jarvis_migrator",
            password="migration-contract-password",
            min_size=1,
            max_size=1,
        )
        try:
            from jarvis_common.migrations import run_migrations

            await run_migrations(migrator_pool, migrations_dir=db_dir / "migrations")
        finally:
            await migrator_pool.close()

        await bootstrap.execute(
            "REVOKE jarvis_platform_owner, jarvis_research_owner, jarvis_learning_owner, "
            "jarvis_ops_owner FROM jarvis_legacy_rollback; "
            "REVOKE ADMIN OPTION FOR jarvis_platform_owner, jarvis_research_owner, "
            "jarvis_learning_owner, jarvis_ops_owner FROM jarvis_migrator; "
            "REVOKE jarvis_legacy_rollback FROM jarvis_migrator"
        )
        recorded_hash = await bootstrap.fetchval(
            "SELECT sha256 FROM ops.schema_migrations WHERE version = 114"
        )
        assert recorded_hash == "2380f76ef37b0c6a0aa15c3a55cffbe7365ede9bfe5d8f22f4c1a72fde334a24"
        assert (
            await bootstrap.fetchval("SELECT sha256 FROM ops.schema_migrations WHERE version = 115")
            == "852dc3ab061d731179d1cd53714887eca328db436eb6e2a4891d98bb1a1e6bcc"
        )
        assert (
            await bootstrap.fetchval("SELECT sha256 FROM ops.schema_migrations WHERE version = 116")
            == "9bbd93a0176f882062a66674cf9897d49473bfdc181c620a4d79131adb99fca6"
        )
        assert (
            await bootstrap.fetchval("SELECT sha256 FROM ops.schema_migrations WHERE version = 117")
            == "d8ff5e67cb30eb0ac0efb6be7e25cc0101b3ee18cd1902e91b8a50cd4954117b"
        )
        assert (
            await bootstrap.fetchval("SELECT sha256 FROM ops.schema_migrations WHERE version = 118")
            == "f12a1b51a1c26225db1d96b1da5cb655584b7938f83bce30ffb638928c3c5468"
        )
        assert (
            await bootstrap.fetchval("SELECT sha256 FROM ops.schema_migrations WHERE version = 119")
            == "9297e47bd11d6cc1547887aa01305261add7c26cabc2ff28ab08ce71497e4296"
        )
        assert (
            await bootstrap.fetchval("SELECT sha256 FROM ops.schema_migrations WHERE version = 120")
            == "2a714b13ecd9fd440d33594c3425e6922999d42bd5f8e47754834ae7e1431847"
        )
        current_version = await bootstrap.fetchval("SELECT max(version) FROM ops.schema_migrations")
        assert current_version == 120
        assert await bootstrap.fetchrow(
            """
            SELECT owner_queue, owner_service FROM ops.procrastinate_jobs
            WHERE args->>'job_id' = 'upgrade-known'
            """
        ) == ("paper_ingestion", "research")
        assert await bootstrap.fetchrow(
            """
            SELECT owner_queue, owner_service FROM ops.procrastinate_jobs
            WHERE args->>'job_id' = 'upgrade-history'
            """
        ) == ("legacy_unknown", "legacy_unknown")
        executor = await bootstrap.fetchrow(
            "SELECT rolcanlogin, rolbypassrls, rolinherit FROM pg_roles "
            "WHERE rolname = 'jarvis_erasure_executor'"
        )
        assert executor == (True, False, False)
        for privilege in ("DELETE", "INSERT", "SELECT", "UPDATE"):
            assert not await bootstrap.fetchval(
                "SELECT has_table_privilege('jarvis_erasure_executor', 'platform.users', $1)",
                privilege,
            )
        assert await bootstrap.fetchval(
            "SELECT has_function_privilege('jarvis_erasure_executor', "
            "'platform.finalize_erasure(uuid)', 'EXECUTE')"
        )
        assert await bootstrap.fetchval(
            "SELECT has_function_privilege('jarvis_erasure_executor', "
            "'platform.due_erasure_request_ids(integer)', 'EXECUTE')"
        )
        assert not await bootstrap.fetchval(
            "SELECT has_table_privilege('jarvis_erasure_executor', "
            "'platform.erasure_requests', 'SELECT')"
        )
        assert not await bootstrap.fetchval(
            """
            SELECT EXISTS (
                SELECT 1
                FROM pg_auth_members AS membership
                JOIN pg_roles AS member ON member.oid = membership.member
                JOIN pg_roles AS granted ON granted.oid = membership.roleid
                WHERE member.rolname = 'jarvis_migrator'
                  AND granted.rolname = 'jarvis_legacy_rollback'
            )
            """
        )
        memberships = await bootstrap.fetch(
            """
            SELECT
                member.rolname AS member_name,
                granted.rolname AS granted_name,
                membership.admin_option
            FROM pg_auth_members AS membership
            JOIN pg_roles AS member ON member.oid = membership.member
            JOIN pg_roles AS granted ON granted.oid = membership.roleid
            WHERE granted.rolname IN (
                'jarvis_platform_owner', 'jarvis_research_owner',
                'jarvis_learning_owner', 'jarvis_ops_owner'
            )
              AND member.rolname IN (
                'jarvis_migrator', 'jarvis_legacy_rollback',
                'jarvis_platform_runtime', 'jarvis_research_runtime',
                'jarvis_learning_runtime'
              )
            ORDER BY member.rolname, granted.rolname
            """
        )
        assert {
            (row["member_name"], row["granted_name"], row["admin_option"]) for row in memberships
        } == {
            ("jarvis_migrator", "jarvis_platform_owner", False),
            ("jarvis_migrator", "jarvis_research_owner", False),
            ("jarvis_migrator", "jarvis_learning_owner", False),
            ("jarvis_migrator", "jarvis_ops_owner", False),
        }
    finally:
        await bootstrap.close()


async def test_0102_webauthn_schema_applied(_contract_pool):
    """0102 created the passkey tables + sessions.credential_id and lifted the floor to 102.

    The session fixture applies db/init.sql (baseline 1..101) then run_migrations(),
    which globs db/migrations/0102_webauthn_credentials.sql. This proves the DDL
    actually PREPARES and executes on a real pg16.8 — a mock cannot catch a bad
    column type or a malformed foreign key.

    Verified: db/migrations/0102_webauthn_credentials.sql; schema version is at least 102.
    """
    async with _contract_pool.acquire() as conn:
        tables = {
            r["table_name"]
            for r in await conn.fetch(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'platform' "
                "AND table_name IN ('webauthn_credentials', 'webauthn_challenges')"
            )
        }
        assert tables == {"webauthn_credentials", "webauthn_challenges"}, (
            f"0102 did not create both passkey tables; found {tables}"
        )

        credential_col = await conn.fetchval(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_schema = 'platform' AND table_name = 'sessions' "
            "AND column_name = 'credential_id'"
        )
        assert credential_col == "uuid", (
            f"sessions.credential_id missing or wrong type: {credential_col!r}"
        )

        max_version = await conn.fetchval("SELECT max(version) FROM ops.schema_migrations")
        assert max_version >= 102, f"schema floor not lifted to 102; max applied = {max_version}"


async def test_0103_purges_only_group_chat_pairings(contract_conn):
    """0103 hard-deletes stale group/supergroup pairings (chat_id < 0), private ones survive.

    Group pairings created before the private-chat-only guard still receive
    outbound scheduled pushes, leaking a user's private content. This
    forward-only data migration purges them. The test executes the SHIPPED
    .sql text (not a re-derived DELETE) against real Postgres, then asserts
    the negative-chat_id row is gone and a positive-chat_id (private) pairing
    for a different user is untouched. It also re-runs the file to prove the
    DELETE is idempotent.

    Runs inside the per-test rollback txn (contract_conn), so it never mutates
    shared state. Repo root is parents[4] from
    libs/jarvis_common/tests/contract/ (parents[5] would resolve above the repo).

    Verified: db/migrations/0103_purge_group_chat_pairings.sql;
    db/init.sql:1311-1316 telegram_user_pairings(user_id bigint, chat_id bigint).
    """
    tag = uuid.uuid4().hex[:8]

    # Two synthetic users (precedent: test_record_author_alert_contract.py:51-54).
    group_user_id: int = await contract_conn.fetchval(
        "INSERT INTO platform.users (email, role) VALUES ($1, 'user') RETURNING id",
        f"pair-group-{tag}@contract.example.com",
    )
    private_user_id: int = await contract_conn.fetchval(
        "INSERT INTO platform.users (email, role) VALUES ($1, 'user') RETURNING id",
        f"pair-private-{tag}@contract.example.com",
    )

    # One stale group pairing (chat_id < 0) and one private pairing (chat_id > 0).
    await contract_conn.execute(
        "INSERT INTO platform.telegram_user_pairings (user_id, chat_id) VALUES ($1, $2)",
        group_user_id,
        -100123456789,
    )
    await contract_conn.execute(
        "INSERT INTO platform.telegram_user_pairings (user_id, chat_id) VALUES ($1, $2)",
        private_user_id,
        123456789,
    )

    # Execute the SHIPPED migration artifact's own SQL, proving the deployed file
    # deletes (a re-derived DELETE here would not).
    sql_path = (
        Path(__file__).resolve().parents[4]
        / "db"
        / "migrations"
        / "0103_purge_group_chat_pairings.sql"
    )
    sql_text = sql_path.read_text()
    await contract_conn.execute("SET LOCAL search_path TO platform, public")
    await contract_conn.execute(sql_text)

    stale_remaining = await contract_conn.fetchval(
        "SELECT COUNT(*) FROM platform.telegram_user_pairings WHERE user_id = $1 AND chat_id = $2",
        group_user_id,
        -100123456789,
    )
    assert stale_remaining == 0, "0103 did not purge the stale group (chat_id < 0) pairing"

    private_remaining = await contract_conn.fetchval(
        "SELECT COUNT(*) FROM platform.telegram_user_pairings WHERE user_id = $1 AND chat_id = $2",
        private_user_id,
        123456789,
    )
    assert private_remaining == 1, "0103 wrongly deleted a private (chat_id > 0) pairing"

    # Idempotent: re-running the same DELETE removes zero further rows and does not error.
    await contract_conn.execute(sql_text)
    negatives_after = await contract_conn.fetchval(
        "SELECT COUNT(*) FROM platform.telegram_user_pairings WHERE chat_id < 0"
    )
    assert negatives_after == 0, "negative-chat_id pairings still present after re-running 0103"


async def test_0104_removes_user_ownership_from_canonical_chunks(contract_conn):
    """Deleting a user cannot cascade into a canonical chunk retained by others."""
    await contract_conn.execute(
        """
        CREATE TEMP TABLE users (
            id integer PRIMARY KEY,
            email text NOT NULL
        ) ON COMMIT DROP;
        CREATE TEMP TABLE paper_chunks (
            id integer PRIMARY KEY,
            paper_id integer NOT NULL,
            content text NOT NULL,
            user_id integer REFERENCES users(id) ON DELETE CASCADE
        ) ON COMMIT DROP;
        CREATE INDEX idx_paper_chunks_user ON paper_chunks (user_id)
            WHERE user_id IS NOT NULL;
        """
    )
    user_id = 41
    await contract_conn.execute(
        "INSERT INTO users (id, email) VALUES ($1, $2)",
        user_id,
        f"chunk-owner-{uuid.uuid4().hex[:8]}@contract.example.com",
    )
    await contract_conn.execute(
        "INSERT INTO paper_chunks (id, paper_id, content, user_id) VALUES (1, 77, $1, $2)",
        "canonical chunk",
        user_id,
    )

    migration = (
        Path(__file__).resolve().parents[4]
        / "db"
        / "migrations"
        / "0104_drop_paper_chunks_user_ownership.sql"
    ).read_text()
    await contract_conn.execute(migration)

    columns = {
        row["attname"]
        for row in await contract_conn.fetch(
            "SELECT attname FROM pg_attribute "
            "WHERE attrelid = 'pg_temp.paper_chunks'::regclass AND attnum > 0 AND NOT attisdropped"
        )
    }
    assert "user_id" not in columns
    legacy_index = await contract_conn.fetchval(
        "SELECT to_regclass('pg_temp.idx_paper_chunks_user')"
    )
    assert legacy_index is None

    await contract_conn.execute("DELETE FROM users WHERE id = $1", user_id)
    surviving_content = await contract_conn.fetchval(
        "SELECT content FROM paper_chunks WHERE id = 1"
    )
    assert surviving_content == "canonical chunk"

    # The shipped migration must also be safe to replay after a partial deploy.
    await contract_conn.execute(migration)


async def test_0104_is_safe_when_the_legacy_chunk_table_is_absent(contract_conn):
    """Minimal or partially restored schemas may not contain the legacy table."""
    await contract_conn.execute(
        "CREATE TEMP TABLE migration_scope_guard (id integer) ON COMMIT DROP"
    )
    await contract_conn.execute("SET LOCAL search_path TO pg_temp")
    migration = (
        Path(__file__).resolve().parents[4]
        / "db"
        / "migrations"
        / "0104_drop_paper_chunks_user_ownership.sql"
    ).read_text()

    await contract_conn.execute(migration)
    await contract_conn.execute(migration)


async def test_0105_backfills_only_one_unambiguous_live_admin(contract_conn):
    """0105 assigns one clear upgrade owner, audits it once, and is replay-safe."""
    await contract_conn.execute(
        """
        CREATE TEMP TABLE users (
            id bigint PRIMARY KEY,
            email text NOT NULL,
            role text NOT NULL,
            deleted_at timestamptz
        ) ON COMMIT DROP;
        CREATE TEMP TABLE user_config (
            id bigserial PRIMARY KEY,
            user_id bigint,
            key text NOT NULL,
            value jsonb,
            created_at timestamptz DEFAULT now(),
            updated_at timestamptz DEFAULT now()
        ) ON COMMIT DROP;
        CREATE UNIQUE INDEX temp_user_config_user_key
            ON user_config (user_id, key) NULLS NOT DISTINCT;
        CREATE TEMP TABLE audit_log (
            id bigserial PRIMARY KEY,
            user_id text,
            action text NOT NULL,
            resource text NOT NULL,
            timestamp timestamptz DEFAULT now(),
            metadata jsonb DEFAULT '{}'::jsonb NOT NULL
        ) ON COMMIT DROP;
        SET LOCAL search_path TO pg_temp;
        INSERT INTO users (id, email, role) VALUES
            (41, 'owner@example.test', 'admin'),
            (42, 'member@example.test', 'user');
        """
    )
    migration = (
        Path(__file__).resolve().parents[4]
        / "db"
        / "migrations"
        / "0105_backfill_owner_user_id.sql"
    ).read_text()

    await contract_conn.execute(migration)
    assert (
        await contract_conn.fetchval(
            "SELECT value FROM user_config WHERE user_id IS NULL AND key = 'owner.user_id'"
        )
        == 41
    )
    assert (
        await contract_conn.fetchval(
            "SELECT COUNT(*) FROM audit_log WHERE action = 'owner.backfilled'"
        )
        == 1
    )

    await contract_conn.execute(migration)
    assert (
        await contract_conn.fetchval(
            "SELECT COUNT(*) FROM audit_log WHERE action = 'owner.backfilled'"
        )
        == 1
    )


async def test_0105_never_guesses_or_overwrites_owner(contract_conn):
    """Zero/multiple admins and any existing owner row remain untouched."""
    await contract_conn.execute(
        """
        CREATE TEMP TABLE users (
            id bigint PRIMARY KEY,
            email text NOT NULL,
            role text NOT NULL,
            deleted_at timestamptz
        ) ON COMMIT DROP;
        CREATE TEMP TABLE user_config (
            id bigserial PRIMARY KEY,
            user_id bigint,
            key text NOT NULL,
            value jsonb,
            created_at timestamptz DEFAULT now(),
            updated_at timestamptz DEFAULT now()
        ) ON COMMIT DROP;
        CREATE UNIQUE INDEX temp_user_config_user_key_ambiguous
            ON user_config (user_id, key) NULLS NOT DISTINCT;
        CREATE TEMP TABLE audit_log (
            id bigserial PRIMARY KEY,
            user_id text,
            action text NOT NULL,
            resource text NOT NULL,
            timestamp timestamptz DEFAULT now(),
            metadata jsonb DEFAULT '{}'::jsonb NOT NULL
        ) ON COMMIT DROP;
        SET LOCAL search_path TO pg_temp;
        INSERT INTO users (id, email, role) VALUES
            (51, 'first@example.test', 'admin'),
            (52, 'second@example.test', 'admin');
        """
    )
    migration = (
        Path(__file__).resolve().parents[4]
        / "db"
        / "migrations"
        / "0105_backfill_owner_user_id.sql"
    ).read_text()

    await contract_conn.execute(migration)
    assert (
        await contract_conn.fetchval(
            "SELECT COUNT(*) FROM user_config WHERE user_id IS NULL AND key = 'owner.user_id'"
        )
        == 0
    )

    await contract_conn.execute(
        "INSERT INTO user_config (user_id, key, value) "
        "VALUES (NULL, 'owner.user_id', '\"malformed\"'::jsonb)"
    )
    await contract_conn.execute("DELETE FROM users WHERE id = 52")
    await contract_conn.execute(migration)
    assert (
        await contract_conn.fetchval(
            "SELECT value FROM user_config WHERE user_id IS NULL AND key = 'owner.user_id'"
        )
        == "malformed"
    )
    assert (
        await contract_conn.fetchval(
            "SELECT COUNT(*) FROM audit_log WHERE action = 'owner.backfilled'"
        )
        == 0
    )


async def test_0106_backfills_only_verified_server_scholarly_rows(contract_conn):
    """0106 promotes verified legacy scholarly rows and keeps all others private."""
    user_id = await contract_conn.fetchval(
        "INSERT INTO platform.users (email) VALUES ('visibility-0106@contract.example.com') RETURNING id"
    )
    rows = [
        ("visibility-0106-arxiv", "arxiv", "user_initiated", user_id),
        ("visibility-0106-openalex", "openalex", "pulse", None),
        ("visibility-0106-citation", "semantic_scholar", "citation_batch", None),
        ("visibility-0106-local", "local", "user_initiated", user_id),
        ("visibility-0106-zotero", "zotero", "user_initiated", user_id),
        ("visibility-0106-unknown", "future_adapter", "user_initiated", None),
    ]
    await contract_conn.executemany(
        """
        INSERT INTO research.papers (
            external_id, source_type, title, authors, url,
            discovery_origin, discovered_by, visibility_scope
        )
        VALUES ($1, $2, 'Visibility 0106', ARRAY['A. Author'],
                'https://example.test/visibility-0106', $3, $4, 'private')
        """,
        rows,
    )

    migration = (
        Path(__file__).resolve().parents[4]
        / "db"
        / "migrations"
        / "0106_paper_visibility_scope.sql"
    ).read_text()
    await contract_conn.execute("SET LOCAL search_path TO research, platform, public")
    await contract_conn.execute(migration)

    result_rows = await contract_conn.fetch(
        """
        SELECT external_id, visibility_scope
        FROM research.papers
        WHERE external_id LIKE 'visibility-0106-%'
        """
    )
    assert {row["external_id"]: row["visibility_scope"] for row in result_rows} == {
        "visibility-0106-arxiv": "public",
        "visibility-0106-openalex": "public",
        "visibility-0106-citation": "private",
        "visibility-0106-local": "private",
        "visibility-0106-zotero": "private",
        "visibility-0106-unknown": "private",
    }
