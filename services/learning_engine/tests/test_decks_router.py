"""Tests for deck CRUD router — H5 / DOM-C-06 per-user scoping."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from learning_engine.routers import decks


class FakeRecord(dict):
    """Dict-like asyncpg.Record substitute."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def get(self, key, default=None):
        return super().get(key, default)


def _now():
    return datetime.now(UTC)


def _make_pool_and_conn():
    conn = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx
    return pool, conn


def _make_deck_row(**overrides):
    values = dict(
        id=1,
        name="Test Deck",
        description=None,
        topic_id=None,
        card_count=0,
        due_count=0,
        created_at=_now(),
    )
    values.update(overrides)
    return FakeRecord(**values)


# ---------------------------------------------------------------------------
# H5: create_deck inserts user_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_deck_passes_user_id_to_insert():
    """create_deck must include user_id as $4 in the INSERT VALUES clause."""
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = _make_deck_row(id=10, name="Physics")

    req = SimpleNamespace(state=SimpleNamespace(user_id=42))
    result = await decks.create_deck.__wrapped__(
        req,
        body=MagicMock(name="Physics", description=None, topic_id=None),
        db_pool=pool,
    )

    assert result.id == 10
    sql, *params = conn.fetchrow.await_args.args
    assert "user_id" in sql.lower()
    assert "INSERT INTO decks" in sql
    # user_id must appear in the VALUES positional args
    assert 42 in params


# ---------------------------------------------------------------------------
# H5: list_decks scopes by user_id (multi-user isolation)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_decks_scopes_to_caller():
    """list_decks must pass user_id to WHERE clause.

    Simulates two users: user A creates deck D; user B's list_decks call
    uses a separate mock conn and returns 0 rows (the DB would filter them
    out). User A's call gets 1 row back. The key assertion is that the SQL
    contains ``user_id IS NOT DISTINCT FROM $1`` and the correct user_id
    is forwarded as a parameter to the DB layer.
    """
    # --- User A ---
    pool_a, conn_a = _make_pool_and_conn()
    conn_a.fetch.return_value = [_make_deck_row(id=1, name="A's Deck")]
    req_a = SimpleNamespace(state=SimpleNamespace(user_id=1))
    rows_a = await decks.list_decks.__wrapped__(req_a, db_pool=pool_a)

    assert len(rows_a) == 1
    sql_a, *params_a = conn_a.fetch.await_args.args
    assert "user_id IS NOT DISTINCT FROM $1" in sql_a
    assert params_a == [1]

    # --- User B ---
    pool_b, conn_b = _make_pool_and_conn()
    conn_b.fetch.return_value = []  # DB returns nothing for user B
    req_b = SimpleNamespace(state=SimpleNamespace(user_id=2))
    rows_b = await decks.list_decks.__wrapped__(req_b, db_pool=pool_b)

    assert len(rows_b) == 0
    sql_b, *params_b = conn_b.fetch.await_args.args
    assert "user_id IS NOT DISTINCT FROM $1" in sql_b
    assert params_b == [2]


@pytest.mark.asyncio
async def test_list_decks_single_user_mode_passes_none():
    """In single-user mode (user_id=None), NULL is forwarded; IS NOT DISTINCT
    FROM NULL still correctly matches only rows where user_id IS NULL."""
    pool, conn = _make_pool_and_conn()
    conn.fetch.return_value = []
    req = SimpleNamespace(state=SimpleNamespace(user_id=None))
    await decks.list_decks.__wrapped__(req, db_pool=pool)

    sql, *params = conn.fetch.await_args.args
    assert "user_id IS NOT DISTINCT FROM $1" in sql
    assert params == [None]
