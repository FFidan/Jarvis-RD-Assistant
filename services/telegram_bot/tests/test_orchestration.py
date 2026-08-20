"""Orchestration workflow tests.

Covers:
- daily_briefing: morning briefing message sent to owner
- deadline_warning: milestone deadline warnings within next 3 days
- review_reminder: spaced repetition due-cards reminder

author_alerts is covered by ``test_author_alerts.py``, and the sends-nothing-
without-a-pairing case for every orchestration by
``test_orchestration_no_pairings.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from jarvis_common.testing import make_bot_config
from jarvis_common.testing_telegram import make_http_response
from pydantic import SecretStr
from telegram_bot.config import BotConfig
from telegram_bot.orchestration import daily_briefing as daily_briefing_mod
from telegram_bot.orchestration import deadline_warning as deadline_warning_mod
from telegram_bot.orchestration import review_reminder as review_reminder_mod
from telegram_bot.platform_client import UserPairing

# ---------------------------------------------------------------------------
# test_daily_briefing
# ---------------------------------------------------------------------------


def _briefing_get_router(*, due_now, total, tasks, milestones, inbox_total=0):
    """Route briefing GETs by URL: feed→total, stats→due_now, tasks, milestones/upcoming.

    The briefing reads the feed twice — once for papers added today, once for
    the inbox view — so the feed branch splits on the ``view`` parameter.
    """

    async def _get(url, *_args, **kwargs):
        if url.endswith("/api/papers/feed"):
            if kwargs.get("params", {}).get("view") == "inbox":
                return make_http_response({"total": inbox_total})
            return make_http_response({"total": total})
        if url.endswith("/api/stats"):
            return make_http_response({"due_now": due_now})
        if url.endswith("/api/tasks"):
            return make_http_response(tasks)
        if url.endswith("/api/milestones/upcoming"):
            return make_http_response(milestones)
        raise AssertionError(f"unexpected GET to {url}")

    return _get


@pytest.mark.asyncio
async def test_daily_briefing_sends_briefing_with_two_papers():
    """run_daily_briefing sends an HTML morning briefing message that includes paper count.

    All product data is now gathered via services_client (REST), so the test
    mocks at the http_client boundary, routing by endpoint.
    """
    bot = AsyncMock()
    config = make_bot_config(BotConfig, jarvis_api_key=SecretStr("secret"))
    pool = AsyncMock()

    now = datetime.now(UTC)
    http_client = AsyncMock(spec=httpx.AsyncClient)
    http_client.get.side_effect = _briefing_get_router(
        due_now=5,
        total=2,
        inbox_total=11,
        tasks=[{"title": "Write paper", "project_name": "ResearchX", "status": "todo"}],
        milestones=[
            {
                "name": "Submit draft",
                "deadline": (now + timedelta(days=3)).isoformat(),
                "project_name": "ResearchX",
            }
        ],
    )

    with patch(
        "telegram_bot.orchestration.daily_briefing.list_user_pairings",
        AsyncMock(return_value=[UserPairing(user_id=1, chat_id=9999)]),
    ):
        await daily_briefing_mod.run_daily_briefing(http_client, pool, bot, config)

    bot.send_message.assert_awaited_once()
    _, kwargs = bot.send_message.await_args
    assert kwargs["chat_id"] == 9999
    assert kwargs["parse_mode"] == "HTML"
    # Message should reference paper count and cards
    text = kwargs["text"]
    assert "2</b> papers added to your library since midnight UTC" in text
    assert "11</b> waiting in your inbox" in text
    assert "5</b> cards due for review right now" in text
    assert "Write paper" in text


@pytest.mark.asyncio
async def test_daily_briefing_reports_unavailable_counts_rather_than_zero():
    """The scheduled briefing states a count it could not read instead of sending a zero."""
    bot = AsyncMock()
    config = make_bot_config(BotConfig, jarvis_api_key=SecretStr("secret"))
    pool = AsyncMock()

    async def _get(url, *_args, **_kwargs):
        if url.endswith("/api/tasks"):
            return make_http_response([{"title": "Write paper", "status": "todo"}])
        if url.endswith("/api/milestones/upcoming"):
            return make_http_response([])
        # Both count endpoints — the paper feed and the stats read — are down.
        return make_http_response(None, status=500)

    http_client = AsyncMock(spec=httpx.AsyncClient)
    http_client.get.side_effect = _get

    with patch(
        "telegram_bot.orchestration.daily_briefing.list_user_pairings",
        AsyncMock(return_value=[UserPairing(user_id=1, chat_id=9999)]),
    ):
        await daily_briefing_mod.run_daily_briefing(http_client, pool, bot, config)

    bot.send_message.assert_awaited_once()
    text = bot.send_message.await_args.kwargs["text"]
    assert "0</b> papers added to your library since midnight UTC" not in text
    assert "0</b> waiting in your inbox" not in text
    assert "0</b> cards due for review right now" not in text
    assert "Papers added to your library since midnight UTC are unavailable right now" in text
    assert "Your inbox count is unavailable right now" in text
    assert "Cards due for review are unavailable right now" in text
    # The sections that were read still render.
    assert "Write paper" in text


@pytest.mark.asyncio
async def test_daily_briefing_passes_owner_headers_on_every_call():
    """Each briefing REST call carries the canonical owner headers."""
    bot = AsyncMock()
    config = make_bot_config(BotConfig, jarvis_api_key=SecretStr("secret"))
    pool = AsyncMock()

    http_client = AsyncMock(spec=httpx.AsyncClient)
    http_client.get.side_effect = _briefing_get_router(due_now=1, total=0, tasks=[], milestones=[])

    with patch(
        "telegram_bot.orchestration.daily_briefing.list_user_pairings",
        AsyncMock(return_value=[UserPairing(user_id=42, chat_id=9999)]),
    ):
        await daily_briefing_mod.run_daily_briefing(http_client, pool, bot, config)

    # Five gathers: feed, inbox feed, stats, tasks, milestones/upcoming.
    assert http_client.get.await_count == 5
    for call in http_client.get.await_args_list:
        headers = call.kwargs["headers"]
        assert "X-API-Key" not in headers
        assert headers["X-Jarvis-Paired-User-Id"] == "42"


# ---------------------------------------------------------------------------
# test_deadline_warning
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deadline_warning_sends_alert_for_upcoming_milestones():
    """run_deadline_warning sends a warning message listing imminent milestones.

    Milestones are now fetched per-pairing via
    ``services_client.fetch_upcoming_milestones`` (GET /api/milestones/upcoming),
    so the test mocks at the http_client boundary.
    """
    bot = AsyncMock()
    config = make_bot_config(BotConfig, jarvis_api_key=SecretStr("secret"))
    pool = AsyncMock()

    now = datetime.now(UTC)
    http_client = AsyncMock(spec=httpx.AsyncClient)
    http_client.get.return_value = make_http_response(
        [
            {
                "name": "Grant submission",
                "deadline": (now + timedelta(days=1)).isoformat(),
                "project_name": "FundingProject",
            }
        ]
    )

    with patch(
        "telegram_bot.orchestration.deadline_warning.list_user_pairings",
        AsyncMock(return_value=[UserPairing(user_id=1, chat_id=9999)]),
    ):
        await deadline_warning_mod.run_deadline_warning(http_client, pool, bot, config)

    # One GET per pairing to the upcoming-milestones endpoint with owner headers.
    http_client.get.assert_awaited_once()
    get_args, get_kwargs = http_client.get.await_args
    assert get_args[0].endswith("/api/milestones/upcoming")
    assert get_kwargs["headers"]["X-Jarvis-Paired-User-Id"] == "1"

    bot.send_message.assert_awaited_once()
    _, kwargs = bot.send_message.await_args
    assert kwargs["chat_id"] == 9999
    assert kwargs["parse_mode"] == "HTML"
    assert "Grant submission" in kwargs["text"]


@pytest.mark.asyncio
async def test_deadline_warning_silent_when_no_milestones():
    """run_deadline_warning sends nothing when pairings exist but no milestones are due."""
    bot = AsyncMock()
    pool = AsyncMock()
    http_client = AsyncMock(spec=httpx.AsyncClient)
    http_client.get.return_value = make_http_response([])

    with patch(
        "telegram_bot.orchestration.deadline_warning.list_user_pairings",
        AsyncMock(return_value=[UserPairing(user_id=1, chat_id=9999)]),
    ):
        await deadline_warning_mod.run_deadline_warning(
            http_client,
            pool,
            bot,
            make_bot_config(BotConfig, jarvis_api_key=SecretStr("secret")),
        )

    bot.send_message.assert_not_awaited()


# ---------------------------------------------------------------------------
# test_review_reminder
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_review_reminder_sends_message_for_due_cards():
    """run_review_reminder sends an inline-button message when cards are due."""
    bot = AsyncMock()
    config = make_bot_config(BotConfig, jarvis_api_key=SecretStr("secret"))
    pool = AsyncMock()

    http_client = AsyncMock(spec=httpx.AsyncClient)
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"due_now": 3}
    http_client.get.return_value = resp

    with patch(
        "telegram_bot.orchestration.review_reminder.list_user_pairings",
        AsyncMock(return_value=[UserPairing(user_id=1, chat_id=9999)]),
    ):
        await review_reminder_mod.run_review_reminder(http_client, pool, bot, config)

    bot.send_message.assert_awaited_once()
    _, kwargs = bot.send_message.await_args
    assert kwargs["chat_id"] == 9999
    assert kwargs["parse_mode"] == "HTML"
    assert "3" in kwargs["text"]
    assert kwargs["reply_markup"] is not None


@pytest.mark.asyncio
async def test_review_reminder_silent_when_no_cards_due():
    """run_review_reminder sends nothing when pairings exist but due_now is 0."""
    bot = AsyncMock()
    pool = AsyncMock()

    http_client = AsyncMock(spec=httpx.AsyncClient)
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"due_now": 0}
    http_client.get.return_value = resp

    with patch(
        "telegram_bot.orchestration.review_reminder.list_user_pairings",
        AsyncMock(return_value=[UserPairing(user_id=1, chat_id=9999)]),
    ):
        await review_reminder_mod.run_review_reminder(
            http_client,
            pool,
            bot,
            make_bot_config(BotConfig, jarvis_api_key=SecretStr("secret")),
        )

    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_review_reminder_skips_when_no_owner():
    """run_review_reminder returns early and sends nothing when no pairings exist."""
    bot = AsyncMock()
    http_client = AsyncMock(spec=httpx.AsyncClient)
    pool = AsyncMock()

    with patch.object(review_reminder_mod, "list_user_pairings", AsyncMock(return_value=[])):
        await review_reminder_mod.run_review_reminder(
            http_client,
            pool,
            bot,
            make_bot_config(BotConfig, jarvis_api_key=SecretStr("secret")),
        )

    bot.send_message.assert_not_awaited()


# ---------------------------------------------------------------------------
# test__owner_headers consolidation
# ---------------------------------------------------------------------------


def test_owner_headers_are_confined_to_the_canonical_client():
    """Migrated orchestrators must delegate canonical owner headers to the client.

    The canonical ``_owner_headers`` lives in ``telegram_bot.config`` (the leaf
    module — transport callers must not drag in the handler chain).
    Migrated orchestrators route every backend call through ``services_client``
    and therefore must not import the helper themselves.
    """

    from telegram_bot import config as config_mod
    from telegram_bot import services_client
    from telegram_bot.config import _owner_headers
    from telegram_bot.orchestration import paper_digest as pd_mod
    from telegram_bot.orchestration import research_pulse as rp_mod
    from telegram_bot.orchestration import review_reminder as rr_mod

    # Backend transport belongs to services_client, not orchestration modules.
    for module in (pd_mod, rp_mod, rr_mod):
        assert not hasattr(module, "_owner_headers"), (
            f"{module.__name__} bypasses the services_client auth boundary"
        )

    # services_client uses the same canonical helper -- no private copy.
    assert services_client._owner_headers is config_mod._owner_headers, (
        "services_client has its own _owner_headers"
    )

    # Smoke-test that the canonical helper produces correct output.
    config = make_bot_config(BotConfig, jarvis_api_key=SecretStr("secret"))
    headers_with_user = _owner_headers(config, 42)
    assert "X-API-Key" not in headers_with_user
    assert headers_with_user["X-Jarvis-Paired-User-Id"] == "42"

    headers_no_user = _owner_headers(config, None)
    assert "X-API-Key" not in headers_no_user
    assert "X-Jarvis-Paired-User-Id" not in headers_no_user
