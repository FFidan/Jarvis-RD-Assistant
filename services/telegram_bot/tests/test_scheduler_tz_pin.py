"""Tests for scheduler timezone resolution via Telegram-paired owner user.

Verifies that reload_nudges uses the personal user.timezone row of the
Telegram-paired owner when present, falling back to the UTC seed when absent.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram_bot.scheduler import JarvisScheduler


def _make_scheduler() -> JarvisScheduler:
    db_pool = MagicMock()
    http_client = MagicMock()
    bot = MagicMock()
    config = MagicMock()
    return JarvisScheduler(db_pool=db_pool, http_client=http_client, bot=bot, config=config)


def _fetchrow_side_effect(*results):
    """Return successive results for each fetchrow call."""
    it = iter(results)

    async def _fn(query, *args):
        return next(it)

    return _fn


# ---------------------------------------------------------------------------
# _resolve_owner_timezone tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_owner_timezone_used_when_present() -> None:
    """When the paired owner has a personal user.timezone row, it is used."""
    scheduler = _make_scheduler()
    # fetchrow call order:
    #   1. telegram.owner_chat_id → {"value": 12345}
    #   2. telegram_user_pairings chat_id=12345 → {"user_id": 7}
    #   3. user.timezone for user_id=7 → {"value": "Europe/Berlin"}
    scheduler.db_pool.fetchrow = AsyncMock(
        side_effect=[
            {"value": 12345},
            {"user_id": 7},
            {"value": "Europe/Berlin"},
        ]
    )
    result = await scheduler._resolve_owner_timezone()
    assert result == "Europe/Berlin"


@pytest.mark.asyncio
async def test_operator_seed_fallback_when_no_pairing() -> None:
    """When owner_chat_id is set but no pairing row exists, the NULL seed is used."""
    scheduler = _make_scheduler()
    scheduler.db_pool.fetchrow = AsyncMock(
        side_effect=[
            {"value": 99999},  # owner_chat_id present
            None,  # no matching pairing row
            {"value": "America/New_York"},  # operator-level seed
        ]
    )
    result = await scheduler._resolve_owner_timezone()
    assert result == "America/New_York"


@pytest.mark.asyncio
async def test_utc_fallback_when_no_config_at_all() -> None:
    """When neither owner chat_id nor seed row exists, 'UTC' is returned."""
    scheduler = _make_scheduler()
    scheduler.db_pool.fetchrow = AsyncMock(
        side_effect=[
            None,  # no telegram.owner_chat_id
            None,  # no operator-level seed
        ]
    )
    result = await scheduler._resolve_owner_timezone()
    assert result == "UTC"


@pytest.mark.asyncio
async def test_utc_fallback_when_owner_has_no_timezone_row() -> None:
    """When owner is paired but has no personal timezone row, falls back to seed."""
    scheduler = _make_scheduler()
    scheduler.db_pool.fetchrow = AsyncMock(
        side_effect=[
            {"value": 42},  # owner_chat_id
            {"user_id": 5},  # pairing found
            None,  # no personal timezone row
            {"value": "Asia/Tokyo"},  # operator-level seed
        ]
    )
    result = await scheduler._resolve_owner_timezone()
    assert result == "Asia/Tokyo"
