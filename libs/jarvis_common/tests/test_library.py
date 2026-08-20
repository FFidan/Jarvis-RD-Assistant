"""Tests for ``jarvis_common.library`` — canonical-corpus helpers.

These tests use mocked asyncpg connections since the real schema requires
a running PostgreSQL instance (see services/paper_ingestion/tests/conftest.py
for the live-PG fixture). The unit tests below assert on captured SQL +
arguments to verify wire-level behaviour.
"""

from __future__ import annotations

from functools import partial
from unittest.mock import AsyncMock

import pytest
from jarvis_common.library import (
    ALLOWED_ADDED_VIA,
    add_to_library,
    fan_out_to_topic_users,
    list_users_with_topic,
)
from jarvis_common.testing import make_conn

_make_conn = partial(
    make_conn,
    execute_return="INSERT 0 1",
    fetch_return=[],
    with_transaction=False,
)


@pytest.mark.asyncio
async def test_add_to_library_binds_owner_capability_arguments():
    """The helper binds the owner, paper, and provenance to one capability call."""
    conn = _make_conn()

    await add_to_library(conn, user_id=42, paper_id=7, added_via="manual_save")

    conn.execute.assert_awaited_once()
    args = conn.execute.await_args.args[1:]
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
    is delegated to Postgres ON CONFLICT DO NOTHING.
    """
    conn = _make_conn()

    await add_to_library(conn, user_id=1, paper_id=2, added_via="manual_save")
    await add_to_library(conn, user_id=1, paper_id=2, added_via="manual_save")

    assert conn.execute.await_count == 2


@pytest.mark.asyncio
async def test_list_users_with_topic_returns_subscribers():
    """Returns user_ids from user_topic_subscriptions for the given topic_id."""
    conn = _make_conn()
    conn.fetch = AsyncMock(return_value=[{"user_id": 3}, {"user_id": 7}])

    users = await list_users_with_topic(conn, topic_id=99)

    assert users == [3, 7]
    conn.fetch.assert_awaited_once()
    sql, arg = conn.fetch.await_args.args
    assert "user_topic_subscriptions" in sql
    assert arg == 99


@pytest.mark.asyncio
async def test_list_users_with_topic_returns_empty_when_no_subscribers():
    """Empty result set maps to an empty list (no subscribers for topic)."""
    conn = _make_conn()
    conn.fetch = AsyncMock(return_value=[])

    users = await list_users_with_topic(conn, topic_id=1)

    assert users == []
    conn.fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_list_users_subscribe_then_fan_out_inserts_library_row(monkeypatch):
    """Subscribe → fan_out_to_topic_users → user_library row exists (seam test)."""
    conn = _make_conn()
    conn.fetchval = AsyncMock(return_value=1)

    # Simulate one subscriber (user 5) for topic 10.
    conn.fetch = AsyncMock(return_value=[{"user_id": 5}])

    count = await fan_out_to_topic_users(conn, paper_id=42, topic_ids=[10])

    assert count == 1
    conn.fetchval.assert_awaited_once()
    assert conn.fetchval.await_args.args[1:] == ([5], 42)
    # Verify topic_id was passed to the subscription query.
    sub_sql, topic_arg = conn.fetch.await_args.args
    assert "user_topic_subscriptions" in sub_sql
    assert topic_arg == 10


@pytest.mark.asyncio
async def test_fan_out_to_topic_users_inserts_one_row_per_user(monkeypatch):
    """Multi-topic fan-out de-duplicates the user set across topics.

    Reviewer note: ``list_users_with_topic`` is currently a no-op (returns
    ``[]``) so we patch it here to verify that *when* per-user topic
    subscriptions ship, the bulk-INSERT shape is still correct. This is a
    regression guard for the owner-capability arguments emitted by
    ``fan_out_to_topic_users``.
    """
    conn = _make_conn()
    conn.fetchval = AsyncMock(return_value=3)

    # Topic 1 → users [1, 2]; Topic 2 → users [2, 3]; union = {1,2,3}
    user_lists = {1: [1, 2], 2: [2, 3]}

    async def fake_list(_db, *, topic_id):  # noqa: ANN001
        return user_lists[topic_id]

    monkeypatch.setattr("jarvis_common.library.list_users_with_topic", fake_list)

    count = await fan_out_to_topic_users(conn, paper_id=42, topic_ids=[1, 2])
    assert count == 3
    conn.fetchval.assert_awaited_once()
    assert conn.fetchval.await_args.args[1:] == ([1, 2, 3], 42)


@pytest.mark.asyncio
async def test_fan_out_to_topic_users_empty_topics_returns_zero():
    """No topics → no fan-out, no DB calls."""
    conn = _make_conn()
    count = await fan_out_to_topic_users(conn, paper_id=42, topic_ids=[])
    assert count == 0
    conn.fetchval.assert_not_awaited()


@pytest.mark.asyncio
async def test_fan_out_to_topic_users_noop_when_list_users_empty():
    """No subscribers for any topic → no INSERT into user_library."""
    conn = _make_conn()
    # _make_conn sets conn.fetch to return [] — no subscribers.
    count = await fan_out_to_topic_users(conn, paper_id=42, topic_ids=[1, 2])
    assert count == 0
    conn.execute.assert_not_awaited()
