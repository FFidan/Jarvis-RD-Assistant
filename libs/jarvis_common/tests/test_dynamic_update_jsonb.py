"""Tests for JC-001 — dynamic_update JSONB double-encode fix.

Two layers of coverage:
1. Mock-level regression guard (no DB required): verifies that dynamic_update
   passes the raw Python dict to asyncpg, NOT a pre-serialised json.dumps string.
   asyncpg's JSONB codec (registered via init_pg_connection) handles serialisation
   itself — calling json.dumps before binding produces a double-encoded
   string-of-a-JSON-string on the wire.

2. Live round-trip test (requires TEST_DATABASE_URL): writes a JSONB value via
   dynamic_update, reads it back with a plain SELECT, and asserts the value is the
   original dict — not a string.
"""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from jarvis_common.db_helpers import dynamic_update

# ---------------------------------------------------------------------------
# Mock-level regression guard (always runs — no DB needed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dynamic_update_passes_raw_dict_not_json_string_for_jsonb():
    """JC-001 regression: dynamic_update must NOT call json.dumps on JSONB values.

    Before the fix, dynamic_update serialised the value itself with json.dumps,
    then asyncpg's codec serialised again → double-encoded string stored in DB.
    After the fix, the raw Python dict is passed to asyncpg directly.
    """
    conn = AsyncMock()
    conn.fetchrow.return_value = {"id": 42, "config": {"foo": 1, "nested": {"x": 2}}}

    payload = {"foo": 1, "nested": {"x": 2}}

    await dynamic_update(
        conn,
        "user_config",
        42,
        updates={"value": payload},
        allowed_columns=frozenset({"value"}),
        jsonb_columns=frozenset({"value"}),
    )

    _sql, *bound_params = conn.fetchrow.await_args.args

    # $1 = record_id, $2 = the JSONB value
    assert len(bound_params) == 2
    jsonb_param = bound_params[1]

    # Must be the raw dict — NOT a json-encoded string
    assert isinstance(jsonb_param, dict), (
        f"JC-001 regression: expected dict, got {type(jsonb_param).__name__!r}: {jsonb_param!r}"
    )
    assert jsonb_param == payload

    # Belt-and-suspenders: it must NOT be the double-encoded form
    assert jsonb_param != json.dumps(payload), (
        "JC-001 regression: dynamic_update is double-encoding the JSONB value"
    )


@pytest.mark.asyncio
async def test_dynamic_update_non_jsonb_columns_pass_value_unchanged():
    """Non-JSONB columns are passed through without any serialisation."""
    conn = AsyncMock()
    conn.fetchrow.return_value = {"id": 1, "name": "hello"}

    await dynamic_update(
        conn,
        "topics",
        1,
        updates={"name": "hello"},
        allowed_columns=frozenset({"name"}),
        # No jsonb_columns — name is TEXT
    )

    _sql, *bound_params = conn.fetchrow.await_args.args
    assert bound_params == [1, "hello"]


# ---------------------------------------------------------------------------
# Live round-trip test (skipped when TEST_DATABASE_URL is not set)
# ---------------------------------------------------------------------------

_DB_URL = os.environ.get("TEST_DATABASE_URL", "")

pytestmark_live = pytest.mark.skipif(
    not _DB_URL,
    reason="TEST_DATABASE_URL not set — skipping live DB round-trip test",
)


@pytest_asyncio.fixture(scope="function")
async def db_pool_with_config_table():
    """Spin up an asyncpg pool against the test DB and create a temp config table."""
    if not _DB_URL:
        pytest.skip("TEST_DATABASE_URL not set")

    import asyncpg
    from jarvis_common import init_pg_connection

    try:
        pool = await asyncpg.create_pool(
            _DB_URL,
            min_size=1,
            max_size=2,
            init=init_pg_connection,
        )
    except Exception as exc:
        pytest.skip(f"Cannot connect to test DB: {exc}")
        return  # unreachable

    # Create an isolated table that mirrors user_config structure
    table = "jc001_test_config"
    async with pool.acquire() as conn:
        await conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {table} (
                id    SERIAL PRIMARY KEY,
                key   TEXT NOT NULL,
                value JSONB
            )
        """)
        await conn.execute(f"DELETE FROM {table}")

    yield pool, table

    async with pool.acquire() as conn:
        await conn.execute(f"DROP TABLE IF EXISTS {table}")

    await pool.close()


@pytest.mark.asyncio
@pytestmark_live
async def test_dynamic_update_jsonb_round_trip(db_pool_with_config_table):
    """Write a nested dict via dynamic_update, read it back, assert no double-encoding.

    If JC-001 regresses, the SELECT would return a JSON string instead of a dict,
    and json.loads() would be required to recover the original value.
    """
    pool, table = db_pool_with_config_table
    original = {"foo": 1, "nested": {"x": 2}, "flag": True}

    async with pool.acquire() as conn:
        # Insert a row to update
        row_id = await conn.fetchval(
            f"INSERT INTO {table} (key, value) VALUES ($1, $2::jsonb) RETURNING id",
            "test_key",
            original,
        )

        # Update via dynamic_update (the function under test)
        updated_payload = {"foo": 99, "nested": {"x": 7}, "flag": False}
        await dynamic_update(
            conn,
            table,
            row_id,
            updates={"value": updated_payload},
            allowed_columns=frozenset({"value"}),
            jsonb_columns=frozenset({"value"}),
        )

        # Read back with a plain SELECT — asyncpg codec auto-decodes JSONB → dict
        result = await conn.fetchval(f"SELECT value FROM {table} WHERE id = $1", row_id)

    # asyncpg returns native Python types for JSONB — must be a dict, not a string
    assert isinstance(result, dict), (
        f"JC-001 round-trip FAIL: expected dict from JSONB column, got {type(result).__name__!r}. "
        f"Value: {result!r}"
    )
    assert result == updated_payload, (
        f"JC-001 round-trip FAIL: value mismatch.\n"
        f"  Expected: {updated_payload!r}\n"
        f"  Got:      {result!r}"
    )
