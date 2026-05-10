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
async def test_list_users_with_topic_is_noop_until_per_user_topics_ship():
    """Wave-1 reviewer fix: helper returns [] until per-user topic subscriptions exist.

    Previous behaviour fanned out to *all* active users regardless of
    topic_id, which spammed every user with every auto-fetched paper. The
    helper is now a documented no-op; tests assert no DB call is issued.
    """
    conn = _make_conn()
    conn.fetch = AsyncMock()

    users = await list_users_with_topic(conn, topic_id=99)
    assert users == []
    # No DB call — the helper short-circuits without touching the connection.
    conn.fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_users_with_topic_noop_does_not_query_db():
    """Even if the ``users`` table is missing, the no-op never queries — no error."""
    conn = _make_conn()
    conn.fetch = AsyncMock(side_effect=asyncpg.exceptions.UndefinedTableError("users"))

    users = await list_users_with_topic(conn, topic_id=1)
    assert users == []
    conn.fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_fan_out_to_topic_users_inserts_one_row_per_user(monkeypatch):
    """Multi-topic fan-out de-duplicates the user set across topics.

    Reviewer note: ``list_users_with_topic`` is currently a no-op (returns
    ``[]``) so we patch it here to verify that *when* per-user topic
    subscriptions ship, the bulk-INSERT shape is still correct. This is a
    regression guard for the SQL emitted by ``fan_out_to_topic_users``.
    """
    conn = _make_conn()

    # Topic 1 → users [1, 2]; Topic 2 → users [2, 3]; union = {1,2,3}
    user_lists = {1: [1, 2], 2: [2, 3]}

    async def fake_list(_db, *, topic_id):  # noqa: ANN001
        return user_lists[topic_id]

    monkeypatch.setattr("jarvis_common.library.list_users_with_topic", fake_list)

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
async def test_fan_out_to_topic_users_noop_when_list_users_empty():
    """Default no-op state: ``list_users_with_topic`` returns ``[]`` → no INSERT."""
    conn = _make_conn()
    count = await fan_out_to_topic_users(conn, paper_id=42, topic_ids=[1, 2])
    assert count == 0
    conn.execute.assert_not_awaited()
