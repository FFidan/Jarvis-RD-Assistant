"""Audit log shared contract suite — Phase E1.JC.

Exercises ``jarvis_common.audit.log_audit`` against a real DB (contract_conn)
so the DB write path is proven rather than mocked.

Tables used:
  audit_log — id, user_id, action, resource, timestamp, metadata
  Verified: db/init.sql:478-485 at HEAD.

Covered:
  1. log_audit writes a row with the correct user_id, action, resource.
  2. Metadata is stored as JSONB and read back correctly.
  3. Oversized metadata is truncated to the marker dict (not stored raw).
  4. log_audit never raises — DB-error path swallows exceptions.

Supersedes: mock-unit tests asserting conn.execute called with correct SQL
(test_audit.py::test_log_audit_* family).
"""

from __future__ import annotations

import uuid

import pytest
from jarvis_common.testing import SharedConnPool

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]


# ---------------------------------------------------------------------------
# Helper: build a SharedConnPool-backed fake pool for log_audit
# Verified: audit.py:56-68 — log_audit calls pool.acquire() as conn → execute.
# ---------------------------------------------------------------------------


def _pool(conn):
    return SharedConnPool(conn)


# ---------------------------------------------------------------------------
# Contract tests
# ---------------------------------------------------------------------------


async def test_log_audit_writes_row_with_correct_fields(contract_conn):
    """log_audit inserts a row in audit_log with the supplied user_id, action, resource.

    Verified: audit.py:56-68 — INSERT INTO audit_log (user_id, action, resource, metadata).
    Supersedes: mock-unit test_audit.py::test_log_audit_inserts_row.
    """
    from jarvis_common.audit import log_audit

    uid = f"u-{uuid.uuid4().hex[:8]}"
    action = f"test.action.{uuid.uuid4().hex[:6]}"
    resource = "/api/test-resource"

    await log_audit(_pool(contract_conn), action=action, resource=resource, user_id=uid)

    row = await contract_conn.fetchrow(
        "SELECT user_id, action, resource FROM audit_log WHERE action = $1",
        action,
    )
    assert row is not None, "log_audit did not write a row to audit_log"
    assert row["user_id"] == uid
    assert row["action"] == action
    assert row["resource"] == resource


async def test_log_audit_stores_metadata_as_jsonb(contract_conn):
    """log_audit stores metadata as JSONB; values round-trip correctly.

    Verified: audit.py:56-68 — metadata inserted as $4 (asyncpg handles JSONB).
    Supersedes: mock-unit test_audit.py::test_log_audit_metadata_stored.
    """
    from jarvis_common.audit import log_audit

    action = f"test.meta.{uuid.uuid4().hex[:6]}"
    meta = {"ip": "192.0.2.1", "attempt": 3}

    await log_audit(
        _pool(contract_conn),
        action=action,
        resource="/api/test",
        user_id=None,
        metadata=meta,
    )

    row = await contract_conn.fetchrow(
        "SELECT metadata FROM audit_log WHERE action = $1",
        action,
    )
    assert row is not None
    stored = row["metadata"]
    # asyncpg auto-decodes JSONB → dict; no json.loads needed
    assert stored["ip"] == "192.0.2.1"
    assert stored["attempt"] == 3


async def test_log_audit_truncates_oversized_metadata(contract_conn):
    """Metadata exceeding 4 KB is replaced with a truncation marker.

    Verified: audit.py:18-39 — _cap_metadata: size > _METADATA_MAX_BYTES → truncation marker.
    Verified: audit.py:15 — _METADATA_MAX_BYTES = 4096.
    Supersedes: mock-unit test_audit.py::test_cap_metadata_large.
    """
    from jarvis_common.audit import log_audit

    action = f"test.trunc.{uuid.uuid4().hex[:6]}"
    big_meta = {"data": "x" * 5000}  # clearly > 4 KB

    await log_audit(
        _pool(contract_conn),
        action=action,
        resource="/api/test",
        metadata=big_meta,
    )

    row = await contract_conn.fetchrow(
        "SELECT metadata FROM audit_log WHERE action = $1",
        action,
    )
    assert row is not None
    stored = row["metadata"]
    assert stored.get("_truncated") is True, f"Expected truncation marker; got: {stored}"
    assert "_size" in stored


async def test_log_audit_no_user_id_stores_null(contract_conn):
    """log_audit with user_id=None stores NULL in the user_id column.

    Verified: audit.py:47 — user_id param is str | None; asyncpg stores NULL.
    Supersedes: mock-unit test_audit.py::test_log_audit_no_user.
    """
    from jarvis_common.audit import log_audit

    action = f"test.null_user.{uuid.uuid4().hex[:6]}"

    await log_audit(_pool(contract_conn), action=action, resource="/api/test", user_id=None)

    row = await contract_conn.fetchrow(
        "SELECT user_id FROM audit_log WHERE action = $1",
        action,
    )
    assert row is not None
    assert row["user_id"] is None
