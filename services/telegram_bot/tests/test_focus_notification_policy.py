"""Cross-client focus recovery and scheduled-notification suppression."""

from __future__ import annotations

import importlib
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram_bot.notification_policy import (
    SCHEDULED_NOTIFICATION_KINDS,
    ScheduledNotificationPolicy,
)
from telegram_bot.platform_client import UserPairing
from telegram_bot.scheduler import JOB_REGISTRY, JarvisScheduler


@pytest.mark.parametrize(
    ("kind", "module_name", "function_name"),
    [
        ("daily_summary", "daily_briefing", "run_daily_briefing"),
        ("paper_digest", "paper_digest", "run_paper_digest"),
        ("review_reminder", "review_reminder", "run_review_reminder"),
        ("deadline_warning", "deadline_warning", "run_deadline_warning"),
        ("research_pulse", "research_pulse", "run_research_pulse"),
        ("author_alert", "author_alerts", "run_author_alerts"),
    ],
)
@pytest.mark.asyncio
async def test_every_registered_delivery_is_suppressed_during_focus(
    kind: str,
    module_name: str,
    function_name: str,
) -> None:
    module = importlib.import_module(f"telegram_bot.orchestration.{module_name}")
    policy = MagicMock()
    policy.suppresses = AsyncMock(return_value=True)
    bot = MagicMock()
    bot.send_message = AsyncMock()
    http = MagicMock()

    with patch.object(
        module,
        "list_user_pairings",
        new=AsyncMock(return_value=[UserPairing(user_id=7, chat_id=70)]),
    ):
        await getattr(module, function_name)(
            http,
            MagicMock(),
            bot,
            MagicMock(),
            delivery_policy=policy,
        )

    policy.suppresses.assert_awaited_once_with(7, kind)
    bot.send_message.assert_not_awaited()
    assert not http.method_calls


def test_focus_policy_covers_the_exact_scheduler_registry() -> None:
    assert frozenset(JOB_REGISTRY) == SCHEDULED_NOTIFICATION_KINDS


@pytest.mark.parametrize(
    ("state", "expected"),
    [("active", True), ("paused", True), ("completed", False), (None, False)],
)
@pytest.mark.asyncio
async def test_delivery_policy_uses_authoritative_focus_state(
    state: str | None,
    expected: bool,
) -> None:
    policy = ScheduledNotificationPolicy(MagicMock(), MagicMock())
    session = None if state is None else SimpleNamespace(state=state)
    with patch(
        "telegram_bot.notification_policy.services_client.fetch_active_focus_session",
        new=AsyncMock(return_value=session),
    ):
        assert await policy.suppresses(7, "daily_summary") is expected


@pytest.mark.asyncio
async def test_delivery_policy_fails_closed_when_focus_state_is_unavailable() -> None:
    policy = ScheduledNotificationPolicy(MagicMock(), MagicMock())
    with patch(
        "telegram_bot.notification_policy.services_client.fetch_active_focus_session",
        new=AsyncMock(side_effect=RuntimeError("unavailable")),
    ):
        assert await policy.suppresses(7, "research_pulse") is True


@pytest.mark.asyncio
async def test_scheduler_injects_policy_into_registered_orchestration() -> None:
    scheduler = JarvisScheduler(MagicMock(), MagicMock(), MagicMock(), MagicMock())
    run = AsyncMock()
    module = types.ModuleType("focus_policy_test_orchestration")
    module.run = run

    with (
        patch.dict(sys.modules, {module.__name__: module}),
        patch.dict(JOB_REGISTRY, {"daily_summary": f"{module.__name__}:run"}, clear=True),
    ):
        await scheduler._run_job("daily_summary", 1)

    assert run.await_args.kwargs["delivery_policy"] is scheduler.delivery_policy


@pytest.mark.asyncio
async def test_focus_completion_is_acknowledged_only_after_delivery() -> None:
    bot = MagicMock()
    bot.send_message = AsyncMock()
    scheduler = JarvisScheduler(MagicMock(), MagicMock(), bot, MagicMock())
    session = SimpleNamespace(id=23, recorded_seconds=1500.0, task_id=None)

    with (
        patch(
            "telegram_bot.scheduler.list_user_pairings",
            new=AsyncMock(return_value=[UserPairing(user_id=7, chat_id=70)]),
        ),
        patch(
            "telegram_bot.scheduler.services_client.fetch_pending_telegram_focus_completion",
            new=AsyncMock(return_value=session),
        ),
        patch(
            "telegram_bot.scheduler.services_client.acknowledge_telegram_focus_completion",
            new=AsyncMock(),
        ) as acknowledge,
    ):
        await scheduler._reconcile_focus_sessions()

    bot.send_message.assert_awaited_once()
    acknowledge.assert_awaited_once_with(
        scheduler.http_client,
        scheduler.config,
        7,
        23,
    )


@pytest.mark.asyncio
async def test_focus_completion_is_not_acknowledged_after_failed_delivery() -> None:
    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=RuntimeError("delivery failed"))
    scheduler = JarvisScheduler(MagicMock(), MagicMock(), bot, MagicMock())
    session = SimpleNamespace(id=23, recorded_seconds=1500.0, task_id=None)

    with (
        patch(
            "telegram_bot.scheduler.list_user_pairings",
            new=AsyncMock(return_value=[UserPairing(user_id=7, chat_id=70)]),
        ),
        patch(
            "telegram_bot.scheduler.services_client.fetch_pending_telegram_focus_completion",
            new=AsyncMock(return_value=session),
        ),
        patch(
            "telegram_bot.scheduler.services_client.acknowledge_telegram_focus_completion",
            new=AsyncMock(),
        ) as acknowledge,
    ):
        await scheduler._reconcile_focus_sessions()

    acknowledge.assert_not_awaited()


async def _deliver_completion(bot: MagicMock, session: SimpleNamespace) -> None:
    """Run one completion delivery for *session* through the scheduler."""
    scheduler = JarvisScheduler(MagicMock(), MagicMock(), bot, MagicMock())
    with (
        patch(
            "telegram_bot.scheduler.list_user_pairings",
            new=AsyncMock(return_value=[UserPairing(user_id=7, chat_id=70)]),
        ),
        patch(
            "telegram_bot.scheduler.services_client.fetch_pending_telegram_focus_completion",
            new=AsyncMock(return_value=session),
        ),
        patch(
            "telegram_bot.scheduler.services_client.acknowledge_telegram_focus_completion",
            new=AsyncMock(),
        ),
    ):
        await scheduler._reconcile_focus_sessions()


@pytest.mark.asyncio
async def test_focus_completion_states_the_result_and_offers_both_actions() -> None:
    """A completed session with a task offers to mark it done and to start another."""
    bot = MagicMock()
    bot.send_message = AsyncMock()

    await _deliver_completion(bot, SimpleNamespace(id=23, recorded_seconds=1500.0, task_id=42))

    kwargs = bot.send_message.await_args.kwargs
    assert "?" not in kwargs["text"]
    assert "25 minutes recorded" in kwargs["text"]
    assert [
        button.callback_data for row in kwargs["reply_markup"].inline_keyboard for button in row
    ] == ["task_done_42", "focus_restart"]


@pytest.mark.asyncio
async def test_focus_completion_without_a_task_omits_the_task_button() -> None:
    """Nothing is offered to mark done when the session was not attached to a task."""
    bot = MagicMock()
    bot.send_message = AsyncMock()

    await _deliver_completion(bot, SimpleNamespace(id=23, recorded_seconds=1500.0, task_id=None))

    markup = bot.send_message.await_args.kwargs["reply_markup"]
    assert [button.callback_data for row in markup.inline_keyboard for button in row] == [
        "focus_restart"
    ]
