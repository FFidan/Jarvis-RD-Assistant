"""Scheduler timezone contracts at the Platform and Learning boundaries."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jarvis_common.testing import make_bot_config
from telegram_bot.config import BotConfig
from telegram_bot.platform_client import TelegramRuntime
from telegram_bot.scheduler import NUDGE_REFRESH_INTERVAL_SECONDS, JarvisScheduler
from telegram_bot.services_client import ScheduledNudgePayload


def _make_scheduler() -> JarvisScheduler:
    """Return a scheduler with isolated HTTP, Platform, and Bot doubles."""
    return JarvisScheduler(
        platform_client=MagicMock(),
        http_client=MagicMock(),
        bot=MagicMock(),
        config=make_bot_config(BotConfig),
    )


@pytest.mark.asyncio
async def test_reload_nudges_uses_platform_timezone() -> None:
    """A Platform-resolved personal timezone governs every nudge trigger."""
    scheduler = _make_scheduler()
    runtime = TelegramRuntime(owner_user_id=7, owner_chat_id=70, timezone="Europe/Berlin")
    nudge = ScheduledNudgePayload(id=11, nudge_type="daily_summary", cron_expression="0 8 * * *")

    with (
        patch("telegram_bot.scheduler.get_runtime_context", AsyncMock(return_value=runtime)),
        patch(
            "telegram_bot.scheduler.services_client.fetch_scheduled_nudges",
            AsyncMock(return_value=[nudge]),
        ) as fetch_nudges,
    ):
        await scheduler.reload_nudges()

    job = scheduler.scheduler.get_job("nudge_11")
    assert job is not None
    assert str(job.trigger.timezone) == "Europe/Berlin"
    fetch_nudges.assert_awaited_once_with(scheduler.http_client, scheduler.config, 7)


@pytest.mark.asyncio
async def test_reload_nudges_falls_back_from_unknown_timezone() -> None:
    """An invalid Platform timezone cannot prevent scheduler registration."""
    scheduler = _make_scheduler()
    runtime = TelegramRuntime(owner_user_id=7, owner_chat_id=70, timezone="Invalid/Timezone")
    nudge = ScheduledNudgePayload(id=12, nudge_type="paper_digest", cron_expression="30 7 * * 1")

    with (
        patch("telegram_bot.scheduler.get_runtime_context", AsyncMock(return_value=runtime)),
        patch(
            "telegram_bot.scheduler.services_client.fetch_scheduled_nudges",
            AsyncMock(return_value=[nudge]),
        ),
    ):
        await scheduler.reload_nudges()

    job = scheduler.scheduler.get_job("nudge_12")
    assert job is not None
    assert str(job.trigger.timezone) == "UTC"


@pytest.mark.asyncio
async def test_reload_nudges_skips_learning_without_owner() -> None:
    """No paired owner means no Learning query and no registered nudge."""
    scheduler = _make_scheduler()
    runtime = TelegramRuntime(owner_user_id=None, owner_chat_id=None, timezone="UTC")

    with (
        patch("telegram_bot.scheduler.get_runtime_context", AsyncMock(return_value=runtime)),
        patch(
            "telegram_bot.scheduler.services_client.fetch_scheduled_nudges",
            AsyncMock(),
        ) as fetch_nudges,
    ):
        await scheduler.reload_nudges()

    fetch_nudges.assert_not_awaited()
    assert scheduler.scheduler.get_jobs() == []


@pytest.mark.asyncio
async def test_scheduler_periodically_refreshes_learning_nudges() -> None:
    """The scheduler refreshes without a Research-to-Telegram callback API."""
    scheduler = _make_scheduler()

    with (
        patch.object(scheduler, "reload_nudges", AsyncMock()),
        patch.object(scheduler, "_reconcile_focus_sessions", AsyncMock()),
    ):
        await scheduler.load_and_start()

    try:
        job = scheduler.scheduler.get_job("nudge_refresh")
        assert job is not None
        assert job.trigger.interval.total_seconds() == NUDGE_REFRESH_INTERVAL_SECONDS
    finally:
        await scheduler.stop()
