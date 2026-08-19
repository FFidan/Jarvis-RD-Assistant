"""Audit log shared contract suite.

Exercises ``jarvis_common.audit.log_audit`` through the real Platform runtime
login so both the database write path and caller validation are proven.

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
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import asyncpg
import pytest
import pytest_asyncio
from jarvis_common.db_helpers import init_pg_connection
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


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def audit_runtime_conn(contract_pg_dsn, _contract_pool):
    """Yield a real Platform runtime login against the initialized contract DB."""
    password = "audit-runtime-contract-password"
    bootstrap = await asyncpg.connect(contract_pg_dsn)
    try:
        await bootstrap.execute(
            "ALTER ROLE jarvis_platform_runtime LOGIN PASSWORD 'audit-runtime-contract-password'"
        )
    finally:
        await bootstrap.close()

    runtime = await asyncpg.connect(
        contract_pg_dsn,
        user="jarvis_platform_runtime",
        password=password,
    )
    try:
        await init_pg_connection(runtime)
        await runtime.execute("SET search_path TO platform, ops, public, pg_catalog")
        yield runtime
    finally:
        await runtime.close()


# ---------------------------------------------------------------------------
# Contract tests
# ---------------------------------------------------------------------------


async def test_log_audit_writes_subject_linked_immutable_fact(audit_runtime_conn):
    """log_audit separates mutable identity from the immutable audit fact.

    Verified: audit.py:56-68 — INSERT INTO audit_log (user_id, action, resource, metadata).
    Supersedes: mock-unit test_audit.py::test_log_audit_inserts_row.
    """
    from jarvis_common.audit import log_audit

    uid = str(uuid.uuid4().int % 1_000_000_000 + 1)
    action = f"test.action.{uuid.uuid4().hex[:6]}"
    resource = "/api/test-resource"

    await log_audit(_pool(audit_runtime_conn), action=action, resource=resource, user_id=uid)

    row = await audit_runtime_conn.fetchrow(
        "SELECT user_id, subject_id, caller_role, action, resource "
        "FROM audit_log WHERE action = $1",
        action,
    )
    assert row is not None, "log_audit did not write a row to audit_log"
    assert row["user_id"] is None
    assert row["subject_id"] is not None
    assert row["caller_role"] == "jarvis_platform_runtime"
    assert row["action"] == action
    assert row["resource"] == resource


async def test_log_audit_stores_metadata_as_jsonb(audit_runtime_conn):
    """log_audit stores metadata as JSONB; values round-trip correctly.

    Verified: audit.py:56-68 — metadata inserted as $4 (asyncpg handles JSONB).
    Supersedes: mock-unit test_audit.py::test_log_audit_metadata_stored.
    """
    from jarvis_common.audit import log_audit

    action = f"test.meta.{uuid.uuid4().hex[:6]}"
    meta = {"ip": "192.0.2.1", "attempt": 3}

    await log_audit(
        _pool(audit_runtime_conn),
        action=action,
        resource="/api/test",
        user_id=None,
        metadata=meta,
    )

    row = await audit_runtime_conn.fetchrow(
        "SELECT metadata FROM audit_log WHERE action = $1",
        action,
    )
    assert row is not None
    stored = row["metadata"]
    # asyncpg auto-decodes JSONB → dict; no json.loads needed
    assert stored["attempt"] == 3
    assert "ip" not in stored


async def test_log_audit_truncates_oversized_metadata(audit_runtime_conn):
    """Metadata exceeding 4 KB is replaced with a truncation marker.

    Verified: audit.py:18-39 — _cap_metadata: size > _METADATA_MAX_BYTES → truncation marker.
    Verified: audit.py:15 — _METADATA_MAX_BYTES = 4096.
    Supersedes: mock-unit test_audit.py::test_cap_metadata_large.
    """
    from jarvis_common.audit import log_audit

    action = f"test.trunc.{uuid.uuid4().hex[:6]}"
    big_meta = {"data": "x" * 5000}  # clearly > 4 KB

    await log_audit(
        _pool(audit_runtime_conn),
        action=action,
        resource="/api/test",
        metadata=big_meta,
    )

    row = await audit_runtime_conn.fetchrow(
        "SELECT metadata FROM audit_log WHERE action = $1",
        action,
    )
    assert row is not None
    stored = row["metadata"]
    assert stored.get("_truncated") is True, f"Expected truncation marker; got: {stored}"
    assert "_size" in stored


async def test_log_audit_omits_non_immutable_metadata(audit_runtime_conn):
    """Non-scalar metadata cannot place free-form data in immutable audit facts."""
    from jarvis_common.audit import log_audit

    action = f"test.nonjson.{uuid.uuid4().hex[:6]}"
    at = datetime.now(UTC)
    the_id = uuid.uuid4()

    await log_audit(
        _pool(audit_runtime_conn),
        action=action,
        resource="/api/test",
        metadata={"at": at, "id": the_id},
    )

    row = await audit_runtime_conn.fetchrow(
        "SELECT metadata FROM audit_log WHERE action = $1",
        action,
    )
    assert row is not None, "log_audit silently dropped the row (codec mismatch)"
    stored = row["metadata"]
    assert stored == {}


async def test_log_audit_no_user_id_stores_null(audit_runtime_conn):
    """log_audit with user_id=None stores NULL in the user_id column.

    Verified: audit.py:47 — user_id param is str | None; asyncpg stores NULL.
    Supersedes: mock-unit test_audit.py::test_log_audit_no_user.
    """
    from jarvis_common.audit import log_audit

    action = f"test.null_user.{uuid.uuid4().hex[:6]}"

    await log_audit(_pool(audit_runtime_conn), action=action, resource="/api/test", user_id=None)

    row = await audit_runtime_conn.fetchrow(
        "SELECT user_id FROM audit_log WHERE action = $1",
        action,
    )
    assert row is not None
    assert row["user_id"] is None


async def test_log_audit_hashes_an_unsafe_resource(audit_runtime_conn):
    """Identifiers outside the immutable resource grammar are stored only as a digest."""
    from jarvis_common.audit import log_audit

    action = f"test.resource.{uuid.uuid4().hex[:6]}"
    unsafe_resource = f"9{uuid.uuid4().hex}"
    await log_audit(
        _pool(audit_runtime_conn),
        action=action,
        resource=unsafe_resource,
    )

    stored = await audit_runtime_conn.fetchval(
        "SELECT resource FROM audit_log WHERE action = $1",
        action,
    )
    assert stored.startswith("resource_hash:")
    assert unsafe_resource not in stored


@pytest.mark.parametrize(
    ("actor_id", "resource", "expected"),
    [
        ("1", "milestone:137", "milestone:137"),
        ("2", "project:22", "project:22"),
        ("1", "paper:1001", "paper:1001"),
        ("12", "task:123", "task:123"),
        ("137", "milestone:137", "milestone:subject"),
        ("1", "users/1", "users/subject"),
    ],
)
async def test_log_audit_names_the_object_the_action_touched(
    audit_runtime_conn, actor_id, resource, expected
):
    """The stored resource names the object, not a digit-substring of the actor.

    The last two cases hold the other side of the contract: a segment that *is*
    the actor's id stays pseudonymised, so the append-only row carries no
    erasable identifier.
    Verified: audit.py:78-96 — _immutable_resource replaces whole segments only.
    """
    from jarvis_common.audit import log_audit

    action = f"test.resource.{uuid.uuid4().hex[:8]}"
    await log_audit(_pool(audit_runtime_conn), action=action, resource=resource, user_id=actor_id)

    stored = await audit_runtime_conn.fetchval(
        "SELECT resource FROM audit_log WHERE action = $1",
        action,
    )
    assert stored == expected


async def test_log_audit_keeps_an_attributable_source_for_auth_failures(audit_runtime_conn):
    """An authentication failure records where it came from, without the raw address.

    Verified: auth.py:511,633 — auth.api_key.invalid and auth.session.missing
    pass metadata={"ip": ...}.
    Verified: db/init.sql:2818 — append_audit_event rejects any metadata value
    that is not boolean, number or null, so the address is kept as a digest.
    """
    from jarvis_common.audit import log_audit

    async def source_of_one_failure(address: str) -> int:
        action = f"auth.api_key.invalid.{uuid.uuid4().hex[:8]}"
        await log_audit(
            _pool(audit_runtime_conn),
            action=action,
            resource="/api/test",
            metadata={"ip": address},
        )
        stored = await audit_runtime_conn.fetchval(
            "SELECT metadata FROM audit_log WHERE action = $1",
            action,
        )
        assert stored is not None, "log_audit did not write the authentication-failure row"
        assert "ip" not in stored, "the raw address must not reach the immutable row"
        assert "ip_hash" in stored, "the authentication-failure row has no attributable source"
        return stored["ip_hash"]

    first = await source_of_one_failure("198.51.100.7")
    repeat = await source_of_one_failure("198.51.100.7")
    other = await source_of_one_failure("198.51.100.8")

    assert first == repeat, "repeated attempts from one address must be correlatable"
    assert first != other, "distinct addresses must not collapse onto one value"
    assert first < 2**53, "the audit view transports metadata numbers as doubles"


async def test_log_audit_strict_uses_the_supplied_connection(audit_runtime_conn):
    """Security-critical callers can join the audit insert to their transaction."""
    from jarvis_common.audit import log_audit_strict

    action = f"test.strict.{uuid.uuid4().hex[:8]}"
    await log_audit_strict(
        audit_runtime_conn,
        action=action,
        resource="owner.user_id",
        user_id="17",
        metadata={"reason": "owner_transfer"},
    )

    row = await audit_runtime_conn.fetchrow(
        "SELECT user_id, resource, metadata FROM audit_log WHERE action = $1",
        action,
    )
    assert row is not None
    assert row["user_id"] is None
    assert row["resource"] == "owner.user_id"
    assert row["metadata"] == {}


async def test_log_audit_strict_propagates_insert_failure():
    from jarvis_common.audit import log_audit_strict

    conn = AsyncMock()
    conn.execute.side_effect = RuntimeError("audit unavailable")

    with pytest.raises(RuntimeError, match="audit unavailable"):
        await log_audit_strict(conn, action="owner.transfer", resource="owner.user_id")


async def test_log_audit_strict_rolls_back_with_caller_transaction(audit_runtime_conn):
    from jarvis_common.audit import log_audit_strict

    action = f"test.strict.rollback.{uuid.uuid4().hex[:8]}"
    with pytest.raises(RuntimeError, match="force rollback"):
        async with audit_runtime_conn.transaction():
            await log_audit_strict(
                audit_runtime_conn,
                action=action,
                resource="owner.user_id",
            )
            raise RuntimeError("force rollback")

    assert (
        await audit_runtime_conn.fetchval(
            "SELECT COUNT(*) FROM audit_log WHERE action = $1", action
        )
        == 0
    )


async def test_audit_function_rejects_unsafe_free_form_metadata(audit_runtime_conn):
    """Database validation rejects nested/free-form immutable audit metadata."""
    with pytest.raises(Exception, match="unsafe shape"):
        await audit_runtime_conn.execute(
            "SELECT platform.append_audit_event($1, $2, $3, $4::jsonb)",
            None,
            "test.audit.validation",
            "/api/test",
            {"unbounded_text": "not an immutable fact"},
        )


async def test_runtime_cannot_bypass_the_audit_capability(audit_runtime_conn):
    """Platform runtime cannot omit caller validation with a direct insert."""
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await audit_runtime_conn.execute(
            """INSERT INTO platform.audit_log
               (caller_role, action, resource, metadata)
               VALUES ('jarvis_platform_runtime', 'test.audit.bypass', '/api/test', '{}'::jsonb)"""
        )
