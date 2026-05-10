"""Tests for ``jarvis_common.library`` — Sprint B canonical-corpus helpers.

These tests use mocked asyncpg connections since the real schema requires
a running PostgreSQL instance (see services/paper_ingestion/tests/conftest.py
for the live-PG fixture). The unit tests below assert on captured SQL +
arguments to verify wire-level behaviour.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import asyncpg
import pytest
from jarvis_common.library import (
    ALLOWED_ADDED_VIA,
    add_to_library,
    fan_out_to_topic_users,
    list_users_with_topic,
)


def _make_conn() -> AsyncMock:
    """Mock connection that returns success on every execute/fetch."""
    conn = AsyncMock(spec=asyncpg.Connection)
    conn.execute = AsyncMock(return_value="INSERT 0 1")
    conn.fetch = AsyncMock(return_value=[])
    return conn


@pytest.mark.asyncio
async def test_add_to_library_inserts_with_on_conflict_do_nothing():
    """Happy path — single INSERT with idempotency via ON CONFLICT DO NOTHING."""
    conn = _make_conn()

    await add_to_library(conn, user_id=42, paper_id=7, added_via="manual_save")

    conn.execute.assert_awaited_once()
    sql = conn.execute.await_args.args[0]
    args = conn.execute.await_args.args[1:]
    assert "INSERT INTO user_library" in sql
    assert "ON CONFLICT (user_id, paper_id) DO NOTHING" in sql
    assert args == (42, 7, "manual_save")


@pytest.mark.asyncio
async def test_add_to_library_rejects_invalid_added_via():
    """Defence-in-depth: bad ``added_via`` raises ValueError, not a DB error."""
    conn = _make_conn()
    with pytest.raises(ValueError, match="invalid added_via"):
        await add_to_library(conn, user_id=1, paper_id=2, added_via="bogus")
    conn.execute.assert_not_awaited()


def test_allowed_added_via_matches_migration_check_constraint():
    """Sentinel — keep the Python set in sync with the SQL CHECK constraint."""
    expected = {
        "manual_save",
        "batch_save",
        "zotero_pull",
        "pulse_acceptance",
        "auto_fetch_topic_match",
        "backfill_engagement",
        "backfill_legacy_user_id",
        "topic_discovery",
        "citation_graph",
    }
    assert ALLOWED_ADDED_VIA == expected


@pytest.mark.asyncio
async def test_add_to_library_idempotent_second_call_is_no_op_at_db_level():
    """Calling twice with same args still issues two INSERTs; conflict handling
    is delegated to Postgres ON CONFLICT DO NOTHING."""
    conn = _make_conn()

    await add_to_library(conn, user_id=1, paper_id=2, added_via="manual_save")
    await add_to_library(conn, user_id=1, paper_id=2, added_via="manual_save")

    assert conn.execute.await_count == 2


@pytest.mark.asyncio
async def test_list_users_with_topic_returns_all_active_users():
    """While topics are still global, the helper returns every active user.

    When per-user topic subscriptions land, this assertion will need to be
    refined to filter by the topic_id parameter — that's the explicit intent
    documented in the helper's docstring.
    """
    conn = _make_conn()

    # Mimic the asyncpg.Record interface via dict-like rows.
    class _Row(dict):
        def __getitem__(self, key):
            return super().__getitem__(key)

    conn.fetch = AsyncMock(return_value=[_Row(id=1), _Row(id=2), _Row(id=5)])

    users = await list_users_with_topic(conn, topic_id=99)
    assert users == [1, 2, 5]
    conn.fetch.assert_awaited_once()
    sql = conn.fetch.await_args.args[0]
    assert "FROM users" in sql
    assert "deleted_at IS NULL" in sql


@pytest.mark.asyncio
async def test_list_users_with_topic_returns_empty_when_users_table_missing():
    """Single-tenant deployments don't have a ``users`` table — degrade cleanly."""
    conn = _make_conn()
    conn.fetch = AsyncMock(side_effect=asyncpg.exceptions.UndefinedTableError("users"))

    users = await list_users_with_topic(conn, topic_id=1)
    assert users == []


@pytest.mark.asyncio
async def test_fan_out_to_topic_users_inserts_one_row_per_user():
    """Multi-topic fan-out de-duplicates the user set across topics."""
    conn = _make_conn()

    class _Row(dict):
        def __getitem__(self, key):
            return super().__getitem__(key)

    # Topic 1 → users [1, 2]; Topic 2 → users [2, 3]; union = {1,2,3}
    side_effect_calls = [
        [_Row(id=1), _Row(id=2)],
        [_Row(id=2), _Row(id=3)],
    ]
    conn.fetch = AsyncMock(side_effect=side_effect_calls)

    count = await fan_out_to_topic_users(conn, paper_id=42, topic_ids=[1, 2])
    assert count == 3
    # One bulk INSERT issued
    conn.execute.assert_awaited_once()
    sql = conn.execute.await_args.args[0]
    assert "INSERT INTO user_library" in sql
    assert "auto_fetch_topic_match" in sql
    assert "ON CONFLICT (user_id, paper_id) DO NOTHING" in sql


@pytest.mark.asyncio
async def test_fan_out_to_topic_users_empty_topics_returns_zero():
    """No topics → no fan-out, no DB calls."""
    conn = _make_conn()
    count = await fan_out_to_topic_users(conn, paper_id=42, topic_ids=[])
    assert count == 0
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_fan_out_to_topic_users_no_subscribers_returns_zero():
    """Topic exists but has no subscribers → no INSERT issued."""
    conn = _make_conn()
    conn.fetch = AsyncMock(return_value=[])
    count = await fan_out_to_topic_users(conn, paper_id=42, topic_ids=[1, 2])
    assert count == 0
    conn.execute.assert_not_awaited()
