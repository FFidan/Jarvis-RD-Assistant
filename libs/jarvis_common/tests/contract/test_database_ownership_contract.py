"""Live PostgreSQL contract for installed schemas, ownership, and privileges."""

from __future__ import annotations

import json
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
_DML = ("DELETE", "INSERT", "SELECT", "UPDATE")


def _manifest() -> dict[str, Any]:
    """Load the tracked ownership contract."""
    return json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))


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
    ):
        await contract_conn.fetchval(f"SELECT COUNT(*) FROM {relation}")
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


async def test_owner_default_privileges_apply_to_new_tables(
    contract_conn: asyncpg.Connection,
) -> None:
    """Objects created by an owner inherit the declared runtime grant policy.

    Verified: db/migrations/0114_owned_schemas_and_roles.sql — owner default privileges.
    """
    await contract_conn.execute("SET LOCAL ROLE jarvis_platform_owner")
    await contract_conn.execute(
        "CREATE TABLE platform.ownership_default_privilege_probe (id integer PRIMARY KEY)"
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
