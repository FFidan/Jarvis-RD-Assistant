"""Real-PostgreSQL contract for the declared database role matrix.

The current schema still uses the compatibility login. This test projects the
declared owner and runtime grants inside the fixture's rollback transaction, so
the final access contract is executable before production grants are applied.
"""

from __future__ import annotations

import json
import re
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
_IDENTIFIER_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
_DML_PRIVILEGES = ("DELETE", "INSERT", "SELECT", "UPDATE")


def _load_manifest() -> dict[str, Any]:
    """Load the database ownership contract used by the live projection."""
    return json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))


def _quote_identifier(identifier: str) -> str:
    """Quote a manifest identifier after enforcing its restricted grammar.

    Parameters
    ----------
    identifier : str
        Role, schema, or relation identifier from the tracked manifest.

    Returns
    -------
    str
        Double-quoted PostgreSQL identifier.

    Raises
    ------
    ValueError
        If the identifier is outside the manifest's restricted grammar.

    """
    if _IDENTIFIER_RE.fullmatch(identifier) is None:
        raise ValueError(f"unsupported database identifier: {identifier!r}")
    return f'"{identifier}"'


async def _ensure_declared_roles(
    conn: asyncpg.Connection,
    manifest: dict[str, Any],
) -> tuple[str, ...]:
    """Create absent declared roles inside the rollback transaction.

    Parameters
    ----------
    conn : asyncpg.Connection
        Superuser test connection wrapped by the rollback fixture.
    manifest : dict[str, Any]
        Parsed ownership manifest.

    Returns
    -------
    tuple[str, ...]
        Sorted owner and runtime role names participating in table access.

    """
    roles = {
        role
        for domain in manifest["domains"].values()
        for role in (domain["owner_role"], domain["runtime_role"])
        if role is not None
    }
    for role in sorted(roles):
        exists = await conn.fetchval(
            "SELECT EXISTS (SELECT FROM pg_roles WHERE rolname = $1)", role
        )
        if not exists:
            await conn.execute(f"CREATE ROLE {_quote_identifier(role)} NOLOGIN")
    return tuple(sorted(roles))


async def _table_location(
    conn: asyncpg.Connection,
    table: str,
    destination_schema: str,
) -> tuple[str, str]:
    """Resolve a manifest table to its current physical schema.

    Parameters
    ----------
    conn : asyncpg.Connection
        Live PostgreSQL connection containing the current schema.
    table : str
        Unqualified manifest table name.
    destination_schema : str
        Declared destination schema, preferred after schema cutover.

    Returns
    -------
    tuple[str, str]
        Physical schema and qualified relation text.

    Raises
    ------
    AssertionError
        If no physical table matches the manifest entry.

    """
    schema = await conn.fetchval(
        """SELECT n.nspname
             FROM pg_class AS c
             JOIN pg_namespace AS n ON n.oid = c.relnamespace
            WHERE c.relname = $1 AND c.relkind IN ('r', 'p')
            ORDER BY CASE n.nspname WHEN $2 THEN 0 WHEN 'public' THEN 1 ELSE 2 END
            LIMIT 1""",
        table,
        destination_schema,
    )
    assert schema is not None, f"manifest table is absent from PostgreSQL: {table}"
    qualified = f"{_quote_identifier(schema)}.{_quote_identifier(table)}"
    return schema, qualified


async def test_declared_table_roles_allow_owner_and_reject_foreign_domains(
    contract_conn: asyncpg.Connection,
) -> None:
    """Project final table grants and verify every ownership row.

    Parameters
    ----------
    contract_conn : asyncpg.Connection
        Real PostgreSQL connection whose outer transaction is rolled back.

    """
    # Verified: db/ownership-manifest.json:27-237 — role and object declarations.
    manifest = _load_manifest()
    declared_roles = await _ensure_declared_roles(contract_conn, manifest)
    role_list = ", ".join(_quote_identifier(role) for role in declared_roles)

    for domain_name, tables in manifest["tables"].items():
        domain = manifest["domains"][domain_name]
        allowed_roles = {domain["owner_role"]}
        if domain["runtime_role"] is not None:
            allowed_roles.add(domain["runtime_role"])

        for table in tables:
            _, qualified = await _table_location(contract_conn, table, domain["schema"])
            await contract_conn.execute(f"REVOKE ALL ON TABLE {qualified} FROM PUBLIC, {role_list}")
            for role in sorted(allowed_roles):
                await contract_conn.execute(
                    f"GRANT {', '.join(_DML_PRIVILEGES)} ON TABLE {qualified} "
                    f"TO {_quote_identifier(role)}"
                )

            for role in declared_roles:
                for privilege in _DML_PRIVILEGES:
                    permitted = await contract_conn.fetchval(
                        "SELECT has_table_privilege($1, $2, $3)",
                        role,
                        qualified,
                        privilege,
                    )
                    assert permitted is (role in allowed_roles), (
                        f"{role} privilege {privilege} on {qualified} was {permitted}; "
                        f"expected {role in allowed_roles}"
                    )
