"""Regression tests for auth_check — telegram_user_pairings lookup (C1 fix).

Covers:
- test_auth_check_accepts_paired_user: chat_id in telegram_user_pairings -> True
- test_auth_check_rejects_unpaired_unknown_chat: chat_id not in any table -> False
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from jarvis_common.testing import make_bot_config, make_telegram_update
from telegram_bot.handlers.helpers import auth_check as _auth_check


def _make_pool(*, fetchval_return=None, fetchrow_return=None) -> MagicMock:
    """Build a minimal pool mock covering both DB lookups in auth_check."""
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=fetchval_return)
    pool.fetchrow = AsyncMock(return_value=fetchrow_return)
    return pool


# ---------------------------------------------------------------------------
# C1 regression tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_check_accepts_paired_user():
    """chat_id present in telegram_user_pairings grants access (priority 3)."""
    # Simulate: user_config owner row is None, but pairing row exists.
    pairing_row = {"user_id": 1}
    pool = _make_pool(fetchval_return=None, fetchrow_return=pairing_row)
    update = make_telegram_update(chat_id=999)
    config = make_bot_config(telegram_chat_id=None)

    authorized, user_id = await _auth_check(update, config, pool)

    assert authorized is True
    assert user_id == 1
    # Ensure the pairing query was actually made
    pool.fetchrow.assert_awaited_once()
    call_sql = pool.fetchrow.await_args.args[0]
    assert "telegram_user_pairings" in call_sql
    assert pool.fetchrow.await_args.args[1] == 999


@pytest.mark.asyncio
async def test_auth_check_rejects_unpaired_unknown_chat():
    """chat_id absent from env, user_config, and pairings table -> False."""
    pool = _make_pool(fetchval_return=None, fetchrow_return=None)
    update = make_telegram_update(chat_id=12345)
    config = make_bot_config(telegram_chat_id=None)

    authorized, user_id = await _auth_check(update, config, pool)

    assert authorized is False
    assert user_id is None
    # Both DB paths should have been consulted
    pool.fetchval.assert_awaited_once()
    pool.fetchrow.assert_awaited_once()


@pytest.mark.asyncio
async def test_auth_check_returns_user_id_for_paired_chat():
    """Wave-0 C1: paired chats expose the DB user_id for downstream scoping."""
    pool = _make_pool(fetchval_return=None, fetchrow_return={"user_id": 7})
    update = make_telegram_update(chat_id=999)
    config = make_bot_config(telegram_chat_id=None)

    assert await _auth_check(update, config, pool) == (True, 7)


@pytest.mark.asyncio
async def test_auth_check_returns_none_for_owner_match():
    """Wave-0 C1: legacy single-tenant owner_chat_id match returns user_id=None."""
    pool = _make_pool(fetchval_return=999, fetchrow_return=None)
    update = make_telegram_update(chat_id=999)
    config = make_bot_config(telegram_chat_id=None)

    assert await _auth_check(update, config, pool) == (True, None)


@pytest.mark.asyncio
async def test_auth_check_returns_none_for_env_var_match():
    """Wave-0 C1: env-var TELEGRAM_CHAT_ID match returns user_id=None (owner)."""
    pool = _make_pool()
    update = make_telegram_update(chat_id=12345)
    config = make_bot_config(telegram_chat_id=12345)

    assert await _auth_check(update, config, pool) == (True, None)
