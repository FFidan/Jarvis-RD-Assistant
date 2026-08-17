"""Contract tests for the immutable audit-fact rules."""

from __future__ import annotations

import asyncpg
import pytest

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]


async def test_audit_log_delete_is_silently_ignored(
    contract_conn: asyncpg.Connection,
) -> None:
    """The append-only rule must turn direct removal into a no-op."""
    await contract_conn.execute(
        "INSERT INTO audit_log (action, resource, user_id, metadata, caller_role)"
        " VALUES ('test.action', '/test', NULL, '{}', 'jarvis_migrator')"
    )

    await contract_conn.execute("DELETE FROM audit_log WHERE action = 'test.action'")

    count = await contract_conn.fetchval(
        "SELECT count(*) FROM audit_log WHERE action = 'test.action'"
    )
    assert count == 1


async def test_audit_log_update_is_silently_ignored(
    contract_conn: asyncpg.Connection,
) -> None:
    """The append-only rule must turn direct mutation into a no-op."""
    await contract_conn.execute(
        "INSERT INTO audit_log (action, resource, user_id, metadata, caller_role)"
        " VALUES ('test.update', '/test', NULL, '{}', 'jarvis_migrator')"
    )

    await contract_conn.execute(
        "UPDATE audit_log SET action = 'tampered' WHERE action = 'test.update'"
    )

    row = await contract_conn.fetchrow(
        "SELECT action FROM audit_log WHERE action IN ('test.update', 'tampered')"
    )
    assert row is not None and row["action"] == "test.update"
