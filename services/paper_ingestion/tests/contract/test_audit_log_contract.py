"""Contract tests for audit_log append-only invariant (SEC-AUDIT-1).

# Verified: db/migrations/0090_audit_log_append_only.sql:5 (no_delete_audit_log)
# Verified: db/migrations/0090_audit_log_append_only.sql:8 (no_update_audit_log)
"""

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
    """no_delete_audit_log rule must convert removal into a no-op."""
    await contract_conn.execute(
        "INSERT INTO audit_log (action, resource, user_id, metadata)"
        " VALUES ('test.action', '/test', NULL, '{}')"
    )
    await contract_conn.execute("DELETE FROM audit_log WHERE action = 'test.action'")
    count = await contract_conn.fetchval(
        "SELECT count(*) FROM audit_log WHERE action = 'test.action'"
    )
    assert count == 1, "removal rule must leave the row in place"


async def test_audit_log_update_is_silently_ignored(
    contract_conn: asyncpg.Connection,
) -> None:
    """no_update_audit_log rule must convert mutation into a no-op."""
    await contract_conn.execute(
        "INSERT INTO audit_log (action, resource, user_id, metadata)"
        " VALUES ('test.update', '/test', NULL, '{}')"
    )
    await contract_conn.execute(
        "UPDATE audit_log SET action = 'tampered' WHERE action = 'test.update'"
    )
    row = await contract_conn.fetchrow(
        "SELECT action FROM audit_log WHERE action IN ('test.update', 'tampered')"
    )
    assert row is not None and row["action"] == "test.update", (
        "mutation rule must leave the original action intact"
    )
