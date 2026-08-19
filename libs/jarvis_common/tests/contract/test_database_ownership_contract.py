"""Live PostgreSQL contract for installed schemas, ownership, and privileges."""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

import asyncpg
import pytest

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]

_REPO_ROOT = Path(__file__).resolve().parents[4]
_MANIFEST_PATH = _REPO_ROOT / "db" / "ownership-manifest.json"
_JOBS_MIGRATION_PATH = _REPO_ROOT / "db" / "migrations" / "0116_unified_job_facade.sql"
_DML = ("DELETE", "INSERT", "SELECT", "UPDATE")


def _manifest() -> dict[str, Any]:
    """Load the tracked ownership contract."""
    return json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))


async def test_restore_authority_matches_the_versioned_manifest() -> None:
    """Recovery authority is immutable for the declared schema version."""
    manifest = _manifest()
    authority = manifest["restore_authority"]
    authority_path = _REPO_ROOT / authority["path"]
    assert authority["schema_version"] == int(
        (_REPO_ROOT / "db" / "SCHEMA_VERSION").read_text(encoding="utf-8").strip()
    )
    assert hashlib.sha256(authority_path.read_bytes()).hexdigest() == authority["sha256"]


async def _object_owner(
    conn: asyncpg.Connection,
    object_name: str,
    object_kind: str,
) -> tuple[str, str] | None:
    """Return the physical schema and owner for one catalog object."""
    return await conn.fetchrow(
        """
        SELECT namespace.nspname, role.rolname
        FROM pg_class AS relation
        JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
        JOIN pg_roles AS role ON role.oid = relation.relowner
        WHERE relation.relname = $1 AND relation.relkind::text = $2
        """,
        object_name,
        object_kind,
    )


async def test_manifest_objects_have_installed_schema_and_owner(
    contract_conn: asyncpg.Connection,
) -> None:
    """Every manifest table and sequence is physically owned by its domain role.

    Verified: db/ownership-manifest.json:72 — physical domain declarations.
    """
    manifest = _manifest()
    for domain_name, domain in manifest["domains"].items():
        schema = domain["schema"]
        owner = domain["owner_role"]
        for table in manifest["tables"][domain_name]:
            installed = await _object_owner(contract_conn, table, "r")
            assert installed == (schema, owner), f"table {table} is {installed!r}"
        for sequence in manifest["sequences"][domain_name]:
            installed = await _object_owner(contract_conn, sequence, "S")
            assert installed == (schema, owner), f"sequence {sequence} is {installed!r}"


async def test_declared_roles_cannot_bypass_or_assume_owners(
    contract_conn: asyncpg.Connection,
) -> None:
    """Owners cannot log in and runtimes cannot bypass or assume owners.

    Verified: db/ownership-manifest.json:72 — declared owner/runtime roles.
    """
    manifest = _manifest()
    for domain in manifest["domains"].values():
        owner = domain["owner_role"]
        owner_attributes = await contract_conn.fetchrow(
            "SELECT rolcanlogin, rolbypassrls FROM pg_roles WHERE rolname = $1", owner
        )
        assert owner_attributes == (False, False)

        runtime = domain["runtime_role"]
        if runtime is None:
            continue
        runtime_attributes = await contract_conn.fetchrow(
            "SELECT rolbypassrls, rolinherit FROM pg_roles WHERE rolname = $1", runtime
        )
        assert runtime_attributes == (False, False)
        assert not await contract_conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1
                FROM pg_auth_members AS membership
                JOIN pg_roles AS member ON member.oid = membership.member
                JOIN pg_roles AS granted ON granted.oid = membership.roleid
                WHERE member.rolname = $1 AND granted.rolname = $2
            )
            """,
            runtime,
            owner,
        )


async def test_platform_runtime_can_write_its_domain_only(
    contract_conn: asyncpg.Connection,
) -> None:
    """A runtime role can perform granted DML but not foreign-domain DML.

    Verified: db/ownership-manifest.json:72 — Platform and Research domains.
    """
    await contract_conn.execute("SET LOCAL ROLE jarvis_platform_runtime")
    await contract_conn.execute(
        """
        INSERT INTO platform.system_events (level, category, source, message)
        VALUES ('info', 'infra', 'contract-test', 'allowed runtime insert')
        """
    )
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        async with contract_conn.transaction():
            await contract_conn.execute("INSERT INTO research.topics (name) VALUES ('forbidden')")
    await contract_conn.execute("RESET ROLE")


async def test_learning_runtime_retains_approved_research_reads(
    contract_conn: asyncpg.Connection,
) -> None:
    """Learning can inspect source generations without Research write authority."""
    for relation in (
        "research.papers",
        "research.paper_summaries",
        "research.paper_user_state",
        "research.user_library",
    ):
        assert await contract_conn.fetchval(
            "SELECT has_table_privilege($1, $2, 'SELECT')",
            "jarvis_learning_runtime",
            relation,
        )
        assert not await contract_conn.fetchval(
            "SELECT has_table_privilege($1, $2, 'UPDATE')",
            "jarvis_learning_runtime",
            relation,
        )
    await contract_conn.execute("SET LOCAL SESSION AUTHORIZATION jarvis_learning_runtime")
    await contract_conn.fetchval("SELECT content_generation FROM research.papers LIMIT 1")
    await contract_conn.execute("RESET SESSION AUTHORIZATION")

    from jarvis_common.testing import SharedConnPool

    platform_pool = SharedConnPool(contract_conn, session_authorization="jarvis_platform_runtime")
    learning_pool = SharedConnPool(contract_conn, session_authorization="jarvis_learning_runtime")
    await platform_pool.fetchval("SELECT COUNT(*) FROM platform.sessions")
    await learning_pool.fetchval("SELECT content_generation FROM research.papers LIMIT 1")


async def test_runtime_foreign_table_grants_match_the_final_contract(
    contract_conn: asyncpg.Connection,
) -> None:
    """Runtime roles retain only declared foreign reads and queue operations."""
    manifest = _manifest()
    queue_tables = {
        "procrastinate_events",
        "procrastinate_jobs",
        "procrastinate_periodic_defers",
        "procrastinate_workers",
    }
    expected: dict[str, dict[tuple[str, str], set[str]]] = {
        "jarvis_platform_runtime": {
            ("ops", "schema_migrations"): {"SELECT"},
        },
        "jarvis_research_runtime": {
            ("platform", "user_config"): {"SELECT"},
            ("platform", "users"): {"SELECT"},
            **{
                ("learning", table): {"SELECT"}
                for table in {
                    "cards",
                    "daily_log",
                    "decks",
                    "journal_entries",
                    "milestones",
                    "project_papers",
                    "projects",
                    "review_logs",
                    "scheduled_nudges",
                    "task_paper_links",
                    "tasks",
                }
            },
            **{("ops", table): set(_DML) for table in queue_tables},
            ("ops", "schema_migrations"): {"SELECT"},
        },
        "jarvis_learning_runtime": {
            ("platform", "llm_usage_log"): {"SELECT"},
            ("platform", "user_config"): {"SELECT"},
            **{
                ("research", table): {"SELECT"}
                for table in {
                    "paper_chunks",
                    "paper_recommendations",
                    "paper_summaries",
                    "paper_user_state",
                    "paper_user_zotero_links",
                    "papers",
                    "thread",
                    "user_library",
                }
            },
            **{("ops", table): set(_DML) for table in queue_tables},
            ("ops", "schema_migrations"): {"SELECT"},
        },
    }
    runtime_domains = {
        domain["runtime_role"]: name
        for name, domain in manifest["domains"].items()
        if domain["runtime_role"] is not None
    }
    for runtime, own_domain in runtime_domains.items():
        for domain_name, tables in manifest["tables"].items():
            if domain_name == own_domain:
                continue
            schema = manifest["domains"][domain_name]["schema"]
            for table in tables:
                allowed = expected[runtime].get((schema, table), set())
                for privilege in _DML:
                    assert await contract_conn.fetchval(
                        "SELECT has_table_privilege($1, $2, $3)",
                        runtime,
                        f"{schema}.{table}",
                        privilege,
                    ) is (privilege in allowed), f"{runtime} {schema}.{table} {privilege}"

    assert await contract_conn.fetchval(
        "SELECT has_function_privilege($1, $2, 'EXECUTE')",
        "jarvis_research_runtime",
        "platform.audit_readiness_v1()",
    )
    assert await contract_conn.fetchval(
        "SELECT has_function_privilege($1, $2, 'EXECUTE')",
        "jarvis_erasure_executor",
        "platform.due_erasure_request_ids(integer)",
    )
    assert await contract_conn.fetchval(
        "SELECT has_function_privilege($1, $2, 'EXECUTE')",
        "jarvis_research_runtime",
        "learning.update_scheduled_nudge_v1(integer,boolean,text,boolean,boolean,boolean,jsonb)",
    )
    assert not await contract_conn.fetchval(
        "SELECT has_function_privilege('public', $1, 'EXECUTE')",
        "platform.audit_readiness_v1()",
    )
    assert not await contract_conn.fetchval(
        "SELECT has_function_privilege('public', $1, 'EXECUTE')",
        "platform.due_erasure_request_ids(integer)",
    )
    for runtime, function in (
        ("jarvis_platform_runtime", "platform.audit_readiness_v1()"),
        ("jarvis_platform_runtime", "platform.set_research_config_v1(bigint,text,jsonb,text)"),
        ("jarvis_platform_runtime", "platform.finalize_erasure(uuid)"),
        ("jarvis_learning_runtime", "learning.clear_zotero_collection_keys_v1(integer)"),
        (
            "jarvis_learning_runtime",
            "learning.update_scheduled_nudge_v1(integer,boolean,text,boolean,boolean,boolean,jsonb)",
        ),
    ):
        assert not await contract_conn.fetchval(
            "SELECT has_function_privilege($1, $2, 'EXECUTE')", runtime, function
        )
    await contract_conn.execute("SET LOCAL SESSION AUTHORIZATION jarvis_research_runtime")
    try:
        row = await contract_conn.fetchrow(
            "SELECT latest_event_at, event_count FROM platform.audit_readiness_v1()"
        )
        assert row is not None
        assert row["event_count"] >= 0
    finally:
        await contract_conn.execute("RESET SESSION AUTHORIZATION")

    with pytest.raises(asyncpg.RaiseError, match="audit readiness caller is not allowed"):
        async with contract_conn.transaction():
            await contract_conn.execute("SET LOCAL ROLE jarvis_platform_owner")
            await contract_conn.fetchrow(
                "SELECT latest_event_at, event_count FROM platform.audit_readiness_v1()"
            )
    with pytest.raises(asyncpg.RaiseError, match="scheduled nudge caller is not allowed"):
        async with contract_conn.transaction():
            await contract_conn.execute("SET LOCAL ROLE jarvis_learning_owner")
            await contract_conn.fetchrow(
                """
                SELECT * FROM learning.update_scheduled_nudge_v1(
                    1, false, NULL, false, NULL, false, NULL
                )
                """
            )


async def test_backup_reader_can_dump_all_domains_but_cannot_mutate(
    contract_conn: asyncpg.Connection,
) -> None:
    """Scheduled backup authority reads every domain without DML or DDL."""
    await contract_conn.execute("SET LOCAL ROLE jarvis_backup_reader")
    for relation in (
        "platform.users",
        "research.papers",
        "learning.cards",
        "ops.schema_migrations",
        "ops.procrastinate_jobs",
    ):
        await contract_conn.fetchval(f"SELECT COUNT(*) FROM {relation}")
    assert await contract_conn.fetchval(
        "SELECT rolbypassrls FROM pg_roles WHERE rolname = 'jarvis_backup_reader'"
    )
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        async with contract_conn.transaction():
            await contract_conn.execute(
                "INSERT INTO ops.job_progress (jarvis_job_id) VALUES ('forbidden')"
            )
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        async with contract_conn.transaction():
            await contract_conn.execute("CREATE TABLE research.forbidden_backup_ddl (id int)")
    await contract_conn.execute("RESET ROLE")


async def test_runtime_login_cannot_assume_an_owner(live_pg_dsn: str) -> None:
    """A runtime login receives no owner-role membership.

    Verified: db/migrations/0114_owned_schemas_and_roles.sql — runtime role policy.
    """
    bootstrap = await asyncpg.connect(live_pg_dsn)
    try:
        await bootstrap.execute((_REPO_ROOT / "db" / "init.sql").read_text(encoding="utf-8"))
        await bootstrap.execute(
            "ALTER ROLE jarvis_platform_runtime LOGIN PASSWORD 'ownership-contract-password'"
        )
        runtime = await asyncpg.connect(
            live_pg_dsn,
            user="jarvis_platform_runtime",
            password="ownership-contract-password",
        )
        try:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await runtime.execute("SET ROLE jarvis_platform_owner")
        finally:
            await runtime.close()
    finally:
        await bootstrap.close()


async def test_restore_authority_reapplies_current_privileges(live_pg_dsn: str) -> None:
    """The recovery contract restores grants only at the packaged schema."""
    bootstrap = await asyncpg.connect(live_pg_dsn)
    try:
        await bootstrap.execute((_REPO_ROOT / "db" / "init.sql").read_text(encoding="utf-8"))
        await bootstrap.execute(
            "REVOKE ALL ON platform.users FROM jarvis_platform_runtime; "
            "ALTER DEFAULT PRIVILEGES FOR ROLE jarvis_platform_owner IN SCHEMA platform "
            "GRANT EXECUTE ON FUNCTIONS TO jarvis_platform_runtime"
        )
        authority_sql = (_REPO_ROOT / "db" / "restore-authority.sql").read_text(encoding="utf-8")
        await bootstrap.execute(
            "\n".join(line for line in authority_sql.splitlines() if not line.startswith("\\"))
        )
        recovery_attributes = await bootstrap.fetch(
            "SELECT rolname, rolbypassrls FROM pg_roles "
            "WHERE rolname IN ('jarvis_backup_reader', 'jarvis_restore_operator') "
            "ORDER BY rolname"
        )
        assert [(row["rolname"], row["rolbypassrls"]) for row in recovery_attributes] == [
            ("jarvis_backup_reader", True),
            ("jarvis_restore_operator", True),
        ]

        # Accounts are readable and creatable by the runtime, but the deletion
        # clock and account removal belong to the owner-defined capabilities:
        # the grace window finalization measures must not be writable by the
        # service that requests an erasure.
        for privilege in ("INSERT", "SELECT"):
            assert await bootstrap.fetchval(
                "SELECT has_table_privilege($1, $2, $3)",
                "jarvis_platform_runtime",
                "platform.users",
                privilege,
            )
        assert not await bootstrap.fetchval(
            "SELECT has_table_privilege($1, $2, 'DELETE')",
            "jarvis_platform_runtime",
            "platform.users",
        )
        assert not await bootstrap.fetchval(
            "SELECT has_column_privilege($1, $2, 'deleted_at', 'UPDATE')",
            "jarvis_platform_runtime",
            "platform.users",
        )
        assert await bootstrap.fetchval(
            "SELECT has_column_privilege($1, $2, 'email', 'UPDATE')",
            "jarvis_platform_runtime",
            "platform.users",
        )
        for table in ("platform.erasure_requests", "platform.erasure_acknowledgements"):
            for privilege in ("INSERT", "UPDATE", "DELETE"):
                assert not await bootstrap.fetchval(
                    "SELECT has_table_privilege($1, $2, $3)",
                    "jarvis_platform_runtime",
                    table,
                    privilege,
                )
        assert await bootstrap.fetchval(
            "SELECT has_function_privilege($1, $2, 'EXECUTE')",
            "jarvis_erasure_executor",
            "platform.due_erasure_request_ids(integer)",
        )
        for runtime in (
            "jarvis_platform_runtime",
            "jarvis_research_runtime",
            "jarvis_learning_runtime",
        ):
            assert not await bootstrap.fetchval(
                "SELECT has_function_privilege($1, $2, 'EXECUTE')",
                runtime,
                "platform.due_erasure_request_ids(integer)",
            )
        await bootstrap.execute("SET ROLE jarvis_platform_owner")
        await bootstrap.execute(
            "CREATE FUNCTION platform.restored_default_function_probe() "
            "RETURNS integer LANGUAGE sql AS 'SELECT 1'"
        )
        await bootstrap.execute("RESET ROLE")
        inherited_execute = await bootstrap.fetchval(
            "SELECT has_function_privilege($1, $2, 'EXECUTE')",
            "jarvis_platform_runtime",
            "platform.restored_default_function_probe()",
        )
        default_acl = await bootstrap.fetch(
            """
            SELECT defaclnamespace::regnamespace::text AS schema, defaclacl::text
            FROM pg_default_acl
            WHERE defaclrole = 'jarvis_platform_owner'::regrole
              AND defaclobjtype = 'f'
            """
        )
        function_acl = await bootstrap.fetchval(
            "SELECT proacl::text FROM pg_proc WHERE oid = $1::regprocedure",
            "platform.restored_default_function_probe()",
        )
        assert not inherited_execute, (default_acl, function_acl)
    finally:
        await bootstrap.close()


async def test_owner_default_privileges_apply_to_new_tables(
    contract_conn: asyncpg.Connection,
) -> None:
    """Owner defaults grant storage access without granting new capabilities.

    Verified: db/migrations/0114_owned_schemas_and_roles.sql — owner default privileges.
    """
    await contract_conn.execute("SET LOCAL ROLE jarvis_platform_owner")
    await contract_conn.execute(
        "CREATE TABLE platform.ownership_default_privilege_probe (id integer PRIMARY KEY)"
    )
    await contract_conn.execute(
        """
        CREATE FUNCTION platform.ownership_default_function_probe()
        RETURNS integer LANGUAGE sql AS 'SELECT 1'
        """
    )
    await contract_conn.execute("RESET ROLE")

    for privilege in _DML:
        assert await contract_conn.fetchval(
            "SELECT has_table_privilege($1, $2, $3)",
            "jarvis_platform_runtime",
            "platform.ownership_default_privilege_probe",
            privilege,
        )
    assert not await contract_conn.fetchval(
        "SELECT has_table_privilege('public', $1, 'SELECT')",
        "platform.ownership_default_privilege_probe",
    )
    assert await contract_conn.fetchval(
        "SELECT has_table_privilege('jarvis_backup_reader', $1, 'SELECT')",
        "platform.ownership_default_privilege_probe",
    )
    assert not await contract_conn.fetchval(
        "SELECT has_table_privilege('jarvis_backup_reader', $1, 'INSERT')",
        "platform.ownership_default_privilege_probe",
    )
    assert not await contract_conn.fetchval(
        "SELECT has_function_privilege($1, $2, 'EXECUTE')",
        "jarvis_platform_runtime",
        "platform.ownership_default_function_probe()",
    )
    assert not await contract_conn.fetchval(
        "SELECT has_function_privilege('public', $1, 'EXECUTE')",
        "platform.ownership_default_function_probe()",
    )


async def test_platform_job_facade_has_capabilities_without_operations_dml(
    contract_conn: asyncpg.Connection,
) -> None:
    """Platform reads jobs only through Operations-owned facade functions."""
    for privilege in _DML:
        assert not await contract_conn.fetchval(
            "SELECT has_table_privilege($1, $2, $3)",
            "jarvis_platform_runtime",
            "ops.procrastinate_jobs",
            privilege,
        )
    for function in (
        "ops.jarvis_job_read_v1(text)",
        "ops.jarvis_job_list_v1(text,text,text,integer)",
        "ops.jarvis_job_cancel_v1(text,text)",
    ):
        assert await contract_conn.fetchval(
            "SELECT has_function_privilege($1, $2, 'EXECUTE')",
            "jarvis_platform_runtime",
            function,
        )
        assert not await contract_conn.fetchval(
            "SELECT has_function_privilege('public', $1, 'EXECUTE')", function
        )
    await contract_conn.execute("SET LOCAL ROLE jarvis_platform_runtime")
    assert (
        await contract_conn.fetch("SELECT * FROM ops.jarvis_job_list_v1(NULL, NULL, NULL, 1)") == []
    )
    await contract_conn.execute("RESET ROLE")


async def test_configuration_delivery_retries_end_in_a_visible_dead_letter(
    contract_conn: asyncpg.Connection,
) -> None:
    """The eighth failed attempt stops automatic retry while retaining diagnostics."""
    from platform_api.repos.config_delivery import due_delivery_ids, record_retry

    delivery_id = uuid.uuid4()
    await contract_conn.execute("SET LOCAL ROLE jarvis_platform_runtime")
    await contract_conn.execute("SET LOCAL search_path TO platform, pg_catalog")
    await contract_conn.execute(
        """INSERT INTO config_deliveries
           (scope_user_id, actor_user_id, key, delivery_id, user_role, state, attempts)
           VALUES (0, 7, 'contract.dead_letter', $1, 'admin', 'pending', 7)""",
        delivery_id,
    )

    assert await record_retry(contract_conn, delivery_id, "Research unavailable")
    assert await contract_conn.fetchrow(
        "SELECT state, attempts, last_error FROM config_deliveries WHERE delivery_id = $1",
        delivery_id,
    ) == ("failed", 8, "Research unavailable")
    assert delivery_id not in await due_delivery_ids(contract_conn)
    await contract_conn.execute("RESET ROLE")


async def test_job_owner_registry_rejects_cross_queue_and_keeps_rollback_view(
    contract_conn: asyncpg.Connection,
) -> None:
    """The durable owner mapping blocks cross-queue writes and exposes rollback rows."""
    job_id = str(uuid.uuid4())
    with pytest.raises(asyncpg.RaiseError, match="queue does not match"):
        async with contract_conn.transaction():
            await contract_conn.execute(
                """
                INSERT INTO ops.procrastinate_jobs (queue_name, task_name, args, status)
                VALUES ('learning_engine', 'paper.process', $1::jsonb, 'todo')
                """,
                {"job_id": job_id, "user_id": 71, "paper_id": 1},
            )

    await contract_conn.execute(
        """
        INSERT INTO ops.procrastinate_jobs (queue_name, task_name, args, status)
        VALUES ('paper_ingestion', 'paper.process', $1::jsonb, 'todo')
        """,
        {"job_id": job_id, "user_id": 71, "paper_id": 1},
    )
    owner = await contract_conn.fetchrow(
        "SELECT owner_queue, owner_service FROM ops.procrastinate_jobs WHERE args->>'job_id' = $1",
        job_id,
    )
    assert owner == ("paper_ingestion", "research")
    await contract_conn.execute("SET LOCAL ROLE jarvis_legacy_rollback")
    rollback_row = await contract_conn.fetchrow(
        "SELECT id, kind, user_id, payload FROM ops.jarvis_jobs_rollback_v1 WHERE id = $1", job_id
    )
    await contract_conn.execute("RESET ROLE")
    assert dict(rollback_row) == {
        "id": job_id,
        "kind": "paper.process",
        "user_id": "71",
        "payload": {"paper_id": 1},
    }
    with pytest.raises(asyncpg.RaiseError, match="queue does not match"):
        async with contract_conn.transaction():
            await contract_conn.execute(
                """
                UPDATE ops.procrastinate_jobs SET queue_name = 'learning_engine'
                WHERE args->>'job_id' = $1
                """,
                job_id,
            )


async def test_job_rows_are_isolated_by_runtime_login(
    contract_conn: asyncpg.Connection,
) -> None:
    """Learning cannot create or mutate a correctly labelled Research job."""
    research_job_id = str(uuid.uuid4())
    learning_job_id = str(uuid.uuid4())
    await contract_conn.execute("SET LOCAL SESSION AUTHORIZATION jarvis_learning_runtime")
    try:
        with pytest.raises(
            (asyncpg.RaiseError, asyncpg.InsufficientPrivilegeError),
            match="another runtime|row-level security",
        ):
            async with contract_conn.transaction():
                await contract_conn.execute(
                    """INSERT INTO ops.procrastinate_jobs
                       (queue_name, task_name, args, status)
                       VALUES ('paper_ingestion', 'paper.process', $1::jsonb, 'todo')""",
                    {"job_id": research_job_id, "user_id": 71, "paper_id": 1},
                )
        await contract_conn.execute(
            """INSERT INTO ops.procrastinate_jobs
               (queue_name, task_name, args, status)
               VALUES ('learning_engine', 'card.generate', $1::jsonb, 'todo')""",
            {"job_id": learning_job_id, "user_id": 71, "paper_id": 1, "deck_id": 1},
        )
        assert (
            await contract_conn.fetchval(
                "SELECT owner_service FROM ops.procrastinate_jobs WHERE args->>'job_id' = $1",
                learning_job_id,
            )
            == "learning"
        )
    finally:
        await contract_conn.execute("RESET SESSION AUTHORIZATION")


async def test_migration_rejects_an_active_unknown_job_before_enforcement(
    contract_conn: asyncpg.Connection,
) -> None:
    """Execute M4's shipped pre-enforcement guard against an active unknown row."""
    migration = _JOBS_MIGRATION_PATH.read_text(encoding="utf-8")
    guard_body = migration.split("DO $$", maxsplit=1)[1].split("$$;", maxsplit=1)[0]
    guard_sql = f"DO $${guard_body}$$;"

    with pytest.raises(asyncpg.RaiseError, match="active application job has no owner mapping"):
        async with contract_conn.transaction():
            await contract_conn.execute("SET LOCAL ROLE jarvis_ops_owner")
            await contract_conn.execute(
                "ALTER TABLE ops.procrastinate_jobs DISABLE TRIGGER procrastinate_jobs_owner_guard_v1"
            )
            await contract_conn.execute(
                """
                INSERT INTO ops.procrastinate_jobs (queue_name, task_name, args, status)
                VALUES ('paper_ingestion', 'legacy.unmapped', $1::jsonb, 'todo')
                """,
                {"job_id": str(uuid.uuid4()), "user_id": 71},
            )
            await contract_conn.execute(guard_sql)
