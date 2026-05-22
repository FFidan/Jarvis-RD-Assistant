"""Predicate-direct contract tests for log_event (A254).

Tests call log_event() directly with a SharedConnPool wrapping contract_conn
so the inserted row is visible within the same transaction and rolled back
after each test — no persistent side effects.

Verified: libs/jarvis_common/jarvis_common/event_log.py:13-64 at HEAD.
Survivor-of (Phase C): scattered log_event call-site mock tests replaced by
this single predicate-direct suite.
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


async def test_a254_log_event_inserts_row_with_correct_fields(contract_conn):
    """A254: log_event inserts a row into system_events with the correct scalar fields.

    Verified: event_log.py:46-58 — INSERT INTO system_events (level, category,
    source, message, context, correlation_id).
    """
    from jarvis_common.event_log import log_event

    pool = SharedConnPool(contract_conn)
    cid = uuid.uuid4()

    await log_event(
        pool=pool,
        level="info",
        category="auth",
        source="test_event_log_contract",
        message="a254_contract_smoke",
        context={"k": "v"},
        correlation_id=cid,
    )

    row = await contract_conn.fetchrow(
        "SELECT level, category, source, message, correlation_id "
        "FROM system_events WHERE message = $1",
        "a254_contract_smoke",
    )
    assert row is not None, "log_event did not insert a row"
    assert row["level"] == "info"
    assert row["category"] == "auth"
    assert row["source"] == "test_event_log_contract"
    assert str(row["correlation_id"]) == str(cid)


async def test_a254_log_event_jsonb_context_not_double_encoded(contract_conn):
    """A254: context dict is stored as JSONB and auto-decoded by asyncpg — not as a string.

    Verified: event_log.py:54 — $5::jsonb cast; asyncpg JSONB codec auto-decodes.
    The key risk is callers accidentally passing a pre-serialised string; we test
    that a plain dict round-trips correctly.
    """
    from jarvis_common.event_log import log_event

    pool = SharedConnPool(contract_conn)
    payload = {"nested": {"x": 1}, "list": [1, 2, 3]}
    marker = f"a254_jsonb_{uuid.uuid4().hex[:8]}"

    await log_event(
        pool=pool,
        level="debug",
        category="job",
        source="test_event_log_contract",
        message=marker,
        context=payload,
    )

    stored = await contract_conn.fetchval(
        "SELECT context FROM system_events WHERE message = $1",
        marker,
    )
    # asyncpg auto-decodes JSONB → dict; if double-encoded it would be a str
    assert isinstance(stored, dict), f"context was not decoded as dict: {type(stored)}"
    assert stored == payload


async def test_a254_log_event_per_source_scoping(contract_conn):
    """A254: multiple log_event calls with different sources produce distinct rows.

    Proves actor/source-column scoping — a query filtered by source returns only
    the rows for that source, not rows from other sources emitted in the same test.
    """
    from jarvis_common.event_log import log_event

    pool = SharedConnPool(contract_conn)
    suffix = uuid.uuid4().hex[:8]
    src_a = f"source_a_{suffix}"
    src_b = f"source_b_{suffix}"
    msg = f"a254_scope_{suffix}"

    await log_event(pool=pool, level="info", category="source", source=src_a, message=msg)
    await log_event(pool=pool, level="warning", category="source", source=src_b, message=msg)

    count_a = await contract_conn.fetchval(
        "SELECT COUNT(*) FROM system_events WHERE source = $1 AND message = $2",
        src_a,
        msg,
    )
    count_b = await contract_conn.fetchval(
        "SELECT COUNT(*) FROM system_events WHERE source = $1 AND message = $2",
        src_b,
        msg,
    )
    assert count_a == 1, f"Expected 1 row for {src_a}, got {count_a}"
    assert count_b == 1, f"Expected 1 row for {src_b}, got {count_b}"
