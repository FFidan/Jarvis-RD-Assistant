"""Contract tests for audit_log append-only invariant.

# Verified: db/migrations/0090_audit_log_append_only.sql:5 (no_delete_audit_log)
# Verified: db/migrations/0090_audit_log_append_only.sql:8 (no_update_audit_log)
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncpg
import pytest

from jarvis_common.testing import SharedConnPool
from paper_ingestion.jobs import data_purge
from paper_ingestion.jobs.data_purge import _anonymize_audit_log_for_users

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


async def test_data_purge_anonymizes_audit_log_rows(
    contract_conn: asyncpg.Connection,
) -> None:
    """GDPR erasure: purge nulls user_id and strips PII metadata, retaining the row."""
    await contract_conn.execute(
        "INSERT INTO audit_log (action, resource, user_id, metadata)"
        " VALUES ('test.purge_me', '/auth', '987654',"
        ' \'{"ip": "203.0.113.7", "client_ip": "203.0.113.7",'
        ' "raw_client_ip": "10.0.0.4", "action_detail": "login"}\')'
    )

    anonymized = await _anonymize_audit_log_for_users(contract_conn, [987654])

    assert anonymized == 1
    row = await contract_conn.fetchrow(
        "SELECT user_id, metadata FROM audit_log WHERE action = 'test.purge_me'"
    )
    assert row is not None, "anonymized row must still exist (operational record retained)"
    assert row["user_id"] is None, "user_id must be nulled"
    raw_metadata = row["metadata"]
    metadata = json.loads(raw_metadata) if isinstance(raw_metadata, str) else raw_metadata
    assert "ip" not in metadata, "PII key must be stripped"
    assert "client_ip" not in metadata, "owner-override client_ip must be stripped"
    assert "raw_client_ip" not in metadata, "owner-override raw_client_ip must be stripped"
    assert metadata.get("action_detail") == "login", "non-PII metadata retained"


async def test_data_purge_task_delete_and_anonymize_are_atomic(
    contract_conn: asyncpg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production ``data_purge_task`` must wrap the user DELETE and audit anonymize atomically.

    audit_log has no FK to users, so a failure landing between the hard DELETE
    and ``_anonymize_audit_log_for_users`` would leave the purged user gone but
    their audit PII retained forever (GDPR breach). This drives the REAL
    ``data_purge_task`` (not a reconstruction) against a pool sharing
    ``contract_conn``, and forces the anonymize step to raise *after* the DELETE
    has run inside the production ``conn.transaction()``. Atomicity must roll the
    DELETE back. This FAILS if the production ``conn.transaction()`` wrap is
    reverted: the DELETE then stays applied and the seeded user disappears.
    """
    uid_raw = await contract_conn.fetchval(
        "INSERT INTO users (email, role, deleted_at)"
        " VALUES ($1, 'user', NOW() - INTERVAL '60 days') RETURNING id",
        "purge-atomic@example.test",
    )
    assert uid_raw is not None
    uid: int = uid_raw
    await contract_conn.execute(
        "INSERT INTO audit_log (action, resource, user_id, metadata)"
        " VALUES ('test.atomic_purge', '/auth', $1,"
        ' \'{"ip": "203.0.113.9", "action_detail": "login"}\')',
        str(uid),
    )

    # Drive the production job against a pool sharing contract_conn, so the job's
    # own conn.transaction() nests as a savepoint on the test's connection.
    pool = SharedConnPool(contract_conn)
    app = SimpleNamespace(state=SimpleNamespace(db_pool=pool, qdrant_client=AsyncMock()))

    # Qdrant purge "succeeds" so the uid is NOT excluded from the hard DELETE,
    # and the anonymize raises INSIDE the production transaction AFTER the DELETE.
    monkeypatch.setattr(data_purge, "_purge_qdrant_for_user", AsyncMock(return_value=0))
    monkeypatch.setattr(
        data_purge,
        "_anonymize_audit_log_for_users",
        AsyncMock(side_effect=RuntimeError("forced rollback mid-purge")),
    )

    # data_purge_task catches the failure internally (logs, never re-raises).
    await data_purge.data_purge_task(app)

    user_row = await contract_conn.fetchrow("SELECT id FROM users WHERE id = $1", uid)
    assert user_row is not None, (
        "hard-DELETE must roll back with the failed anonymize"
        " — FAILS if the production conn.transaction() wrap is removed"
    )

    audit_row = await contract_conn.fetchrow(
        "SELECT user_id, metadata FROM audit_log WHERE action = 'test.atomic_purge'"
    )
    assert audit_row is not None, "audit row must still exist after rollback"
    assert audit_row["user_id"] == str(uid), (
        "audit user_id must NOT remain anonymized after rollback"
    )
    raw_metadata = audit_row["metadata"]
    metadata = json.loads(raw_metadata) if isinstance(raw_metadata, str) else raw_metadata
    assert metadata.get("ip") == "203.0.113.9", "audit PII must be intact when the purge rolls back"


async def test_audit_log_rule_reenabled_after_purge(
    contract_conn: asyncpg.Connection,
) -> None:
    """After the purge bracket, ordinary UPDATEs must again be no-ops."""
    await contract_conn.execute(
        "INSERT INTO audit_log (action, resource, user_id, metadata)"
        " VALUES ('test.reenable', '/auth', '555000', '{}')"
    )
    await _anonymize_audit_log_for_users(contract_conn, [555000])

    # An ordinary UPDATE must still be silently ignored (rule re-enabled).
    await contract_conn.execute(
        "UPDATE audit_log SET action = 'tampered2' WHERE action = 'test.reenable'"
    )
    row = await contract_conn.fetchrow(
        "SELECT action FROM audit_log WHERE action IN ('test.reenable', 'tampered2')"
    )
    assert row is not None and row["action"] == "test.reenable", (
        "rule must be re-enabled so ordinary UPDATE stays a no-op"
    )
