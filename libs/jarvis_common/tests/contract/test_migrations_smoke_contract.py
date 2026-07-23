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
        count_before = await conn.fetchval("SELECT COUNT(*) FROM schema_migrations")

    assert count_before > 0, "no migrations were applied, so idempotence is untested"

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

    db_dir = Path(__file__).resolve().parents[4] / "db"
    migrations_dir = db_dir / "migrations"

    # run_migrations warns and returns on a missing dir, which would make this
    # test pass without executing a single migration.
    assert migrations_dir.is_dir(), f"migrations dir did not resolve: {migrations_dir}"

    # Should not raise any exception
    await run_migrations(_contract_pool, migrations_dir=migrations_dir)


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
        "INSERT INTO users (email, role) VALUES ($1, 'user') RETURNING id",
        f"pair-group-{tag}@contract.example.com",
    )
    private_user_id: int = await contract_conn.fetchval(
        "INSERT INTO users (email, role) VALUES ($1, 'user') RETURNING id",
        f"pair-private-{tag}@contract.example.com",
    )

    # One stale group pairing (chat_id < 0) and one private pairing (chat_id > 0).
    await contract_conn.execute(
        "INSERT INTO telegram_user_pairings (user_id, chat_id) VALUES ($1, $2)",
        group_user_id,
        -100123456789,
    )
    await contract_conn.execute(
        "INSERT INTO telegram_user_pairings (user_id, chat_id) VALUES ($1, $2)",
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
    await contract_conn.execute(sql_text)

    stale_remaining = await contract_conn.fetchval(
        "SELECT COUNT(*) FROM telegram_user_pairings WHERE user_id = $1 AND chat_id = $2",
        group_user_id,
        -100123456789,
    )
    assert stale_remaining == 0, "0103 did not purge the stale group (chat_id < 0) pairing"

    private_remaining = await contract_conn.fetchval(
        "SELECT COUNT(*) FROM telegram_user_pairings WHERE user_id = $1 AND chat_id = $2",
        private_user_id,
        123456789,
    )
    assert private_remaining == 1, "0103 wrongly deleted a private (chat_id > 0) pairing"

    # Idempotent: re-running the same DELETE removes zero further rows and does not error.
    await contract_conn.execute(sql_text)
    negatives_after = await contract_conn.fetchval(
        "SELECT COUNT(*) FROM telegram_user_pairings WHERE chat_id < 0"
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
        "INSERT INTO users (email) VALUES ('visibility-0106@contract.example.com') RETURNING id"
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
        INSERT INTO papers (
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
    await contract_conn.execute(migration)

    result_rows = await contract_conn.fetch(
        """
        SELECT external_id, visibility_scope
        FROM papers
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
