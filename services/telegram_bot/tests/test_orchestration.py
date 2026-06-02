"""Orchestration workflow tests.

Covers:
- author_alerts: alerts when new papers by tracked authors are found
- daily_briefing: morning briefing message sent to owner
- deadline_warning: milestone deadline warnings within next 3 days
- review_reminder: spaced repetition due-cards reminder
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
from telegram_bot.orchestration import author_alerts as author_alerts_mod
from telegram_bot.orchestration import daily_briefing as daily_briefing_mod
from telegram_bot.orchestration import deadline_warning as deadline_warning_mod
from telegram_bot.orchestration import review_reminder as review_reminder_mod

# ---------------------------------------------------------------------------
# test_author_alerts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_author_alerts_sends_message_when_new_paper_found():
    """run_author_alerts sends an HTML alert when a tracked author has a new paper.

    The bot now delegates matching + dedup to the Paper Ingestion service via
    ``services_client.check_authors`` (POST /api/authors/check) and renders one
    message per ``match`` in the response, so the test mocks at the http_client
    boundary.
    """
    bot = AsyncMock()
    config = make_bot_config(BotConfig, telegram_chat_id=9999, jarvis_api_key=SecretStr("secret"))
    pool = AsyncMock()

    tracked_name = "Alice Smith"
    check_resp = make_http_response(
        {
            "matches": [
                {
                    "author_name": tracked_name,
                    "papers": [
                        {
                            "id": 42,
                            "title": "Paper by Alice",
                            "url": "https://example.com/paper",
                        }
                    ],
                }
            ],
            "new_papers": 1,
            "authors_checked": 1,
        }
    )
    http_client = AsyncMock(spec=httpx.AsyncClient)
    http_client.post.return_value = check_resp

    from telegram_bot.owner import UserPairing

    with patch(
        "telegram_bot.owner.list_user_pairings",
        AsyncMock(return_value=[UserPairing(user_id=1, chat_id=9999)]),
    ):
        await author_alerts_mod.run_author_alerts(http_client, pool, bot, config)

    # One POST per pairing to the authors/check endpoint with canonical headers.
    http_client.post.assert_awaited_once()
    post_args, post_kwargs = http_client.post.await_args
    assert post_args[0].endswith("/api/authors/check")
    assert post_kwargs["headers"]["X-Owner-User-Id"] == "1"
    assert post_kwargs["headers"]["X-API-Key"] == "secret"

    bot.send_message.assert_awaited_once()
    _, kwargs = bot.send_message.await_args
    assert kwargs["chat_id"] == 9999
    assert kwargs["parse_mode"] == "HTML"
    assert tracked_name in kwargs["text"]


@pytest.mark.asyncio
async def test_author_alerts_skips_when_no_owner():
    """run_author_alerts returns early and sends nothing when no pairings exist."""
    bot = AsyncMock()
    http_client = AsyncMock(spec=httpx.AsyncClient)
    pool = AsyncMock()

    with patch("telegram_bot.owner.list_user_pairings", AsyncMock(return_value=[])):
        await author_alerts_mod.run_author_alerts(
            http_client,
            pool,
            bot,
            make_bot_config(BotConfig, telegram_chat_id=9999, jarvis_api_key=SecretStr("secret")),
        )

    bot.send_message.assert_not_awaited()


# ---------------------------------------------------------------------------
# test_daily_briefing
# ---------------------------------------------------------------------------


def _briefing_get_router(*, due_now, total, tasks, milestones):
    """Route briefing GETs by URL: feed→total, stats→due_now, tasks, milestones/upcoming."""

    async def _get(url, *_args, **_kwargs):
        if url.endswith("/api/papers/feed"):
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
    config = make_bot_config(BotConfig, telegram_chat_id=9999, jarvis_api_key=SecretStr("secret"))
    pool = AsyncMock()

    now = datetime.now(UTC)
    http_client = AsyncMock(spec=httpx.AsyncClient)
    http_client.get.side_effect = _briefing_get_router(
        due_now=5,
        total=2,
        tasks=[{"title": "Write paper", "project_name": "ResearchX"}],
        milestones=[
            {
                "name": "Submit draft",
                "deadline": (now + timedelta(days=3)).isoformat(),
                "project_name": "ResearchX",
            }
        ],
    )

    from telegram_bot.owner import UserPairing

    with patch(
        "telegram_bot.owner.list_user_pairings",
        AsyncMock(return_value=[UserPairing(user_id=1, chat_id=9999)]),
    ):
        await daily_briefing_mod.run_daily_briefing(http_client, pool, bot, config)

    bot.send_message.assert_awaited_once()
    _, kwargs = bot.send_message.await_args
    assert kwargs["chat_id"] == 9999
    assert kwargs["parse_mode"] == "HTML"
    # Message should reference paper count and cards
    text = kwargs["text"]
    assert "2" in text  # new_papers_count
    assert "5" in text  # due cards


@pytest.mark.asyncio
async def test_daily_briefing_passes_owner_headers_on_every_call():
    """DOM-D-04: each briefing REST call carries the canonical owner headers."""
    bot = AsyncMock()
    config = make_bot_config(BotConfig, telegram_chat_id=9999, jarvis_api_key=SecretStr("secret"))
    pool = AsyncMock()

    http_client = AsyncMock(spec=httpx.AsyncClient)
    http_client.get.side_effect = _briefing_get_router(due_now=1, total=0, tasks=[], milestones=[])

    from telegram_bot.owner import UserPairing

    with patch(
        "telegram_bot.owner.list_user_pairings",
        AsyncMock(return_value=[UserPairing(user_id=42, chat_id=9999)]),
    ):
        await daily_briefing_mod.run_daily_briefing(http_client, pool, bot, config)

    # Four gathers: feed, stats, tasks, milestones/upcoming — all owner-scoped.
    assert http_client.get.await_count == 4
    for call in http_client.get.await_args_list:
        headers = call.kwargs["headers"]
        assert headers["X-API-Key"] == "secret"
        assert headers["X-Owner-User-Id"] == "42"


@pytest.mark.asyncio
async def test_daily_briefing_skips_when_no_owner():
    """run_daily_briefing returns early and sends nothing when no pairings exist."""
    bot = AsyncMock()
    http_client = AsyncMock(spec=httpx.AsyncClient)
    pool = AsyncMock()

    with patch("telegram_bot.owner.list_user_pairings", AsyncMock(return_value=[])):
        await daily_briefing_mod.run_daily_briefing(
            http_client,
            pool,
            bot,
            make_bot_config(BotConfig, telegram_chat_id=9999, jarvis_api_key=SecretStr("secret")),
        )

    bot.send_message.assert_not_awaited()


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
    config = make_bot_config(BotConfig, telegram_chat_id=9999, jarvis_api_key=SecretStr("secret"))
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

    from telegram_bot.owner import UserPairing

    with patch(
        "telegram_bot.owner.list_user_pairings",
        AsyncMock(return_value=[UserPairing(user_id=1, chat_id=9999)]),
    ):
        await deadline_warning_mod.run_deadline_warning(http_client, pool, bot, config)

    # One GET per pairing to the upcoming-milestones endpoint with owner headers.
    http_client.get.assert_awaited_once()
    get_args, get_kwargs = http_client.get.await_args
    assert get_args[0].endswith("/api/milestones/upcoming")
    assert get_kwargs["headers"]["X-Owner-User-Id"] == "1"

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

    from telegram_bot.owner import UserPairing

    with patch(
        "telegram_bot.owner.list_user_pairings",
        AsyncMock(return_value=[UserPairing(user_id=1, chat_id=9999)]),
    ):
        await deadline_warning_mod.run_deadline_warning(
            http_client,
            pool,
            bot,
            make_bot_config(BotConfig, telegram_chat_id=9999, jarvis_api_key=SecretStr("secret")),
        )

    bot.send_message.assert_not_awaited()


# ---------------------------------------------------------------------------
# test_review_reminder
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_review_reminder_sends_message_for_due_cards():
    """run_review_reminder sends an inline-button message when cards are due."""
    bot = AsyncMock()
    config = make_bot_config(BotConfig, telegram_chat_id=9999, jarvis_api_key=SecretStr("secret"))
    pool = AsyncMock()

    http_client = AsyncMock(spec=httpx.AsyncClient)
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"due_now": 3}
    http_client.get.return_value = resp

    from telegram_bot.owner import UserPairing

    with patch(
        "telegram_bot.owner.list_user_pairings",
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

    from telegram_bot.owner import UserPairing

    with patch(
        "telegram_bot.owner.list_user_pairings",
        AsyncMock(return_value=[UserPairing(user_id=1, chat_id=9999)]),
    ):
        await review_reminder_mod.run_review_reminder(
            http_client,
            pool,
            bot,
            make_bot_config(BotConfig, telegram_chat_id=9999, jarvis_api_key=SecretStr("secret")),
        )

    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_review_reminder_skips_when_no_owner():
    """run_review_reminder returns early and sends nothing when no pairings exist."""
    bot = AsyncMock()
    http_client = AsyncMock(spec=httpx.AsyncClient)
    pool = AsyncMock()

    with patch("telegram_bot.owner.list_user_pairings", AsyncMock(return_value=[])):
        await review_reminder_mod.run_review_reminder(
            http_client,
            pool,
            bot,
            make_bot_config(BotConfig, telegram_chat_id=9999, jarvis_api_key=SecretStr("secret")),
        )

    bot.send_message.assert_not_awaited()


# ---------------------------------------------------------------------------
# test__owner_headers consolidation
# ---------------------------------------------------------------------------


def test_owner_headers_all_orchestrators_use_canonical():
    """All orchestrators must emit the canonical owner headers — never a private copy.

    Orchestrators that still issue HTTP calls inline (paper_digest,
    research_pulse, review_reminder) re-export the canonical ``_owner_headers``
    from helpers.  daily_briefing / deadline_warning / author_alerts now route
    every backend call through ``services_client``, which builds the same
    canonical headers internally — so for those the contract is verified at the
    services_client layer (and per-call in the daily_briefing / author_alerts /
    deadline_warning header tests above).
    """

    from telegram_bot import services_client
    from telegram_bot.handlers import helpers as helpers_mod
    from telegram_bot.handlers.helpers import _owner_headers
    from telegram_bot.orchestration import paper_digest as pd_mod
    from telegram_bot.orchestration import research_pulse as rp_mod
    from telegram_bot.orchestration import review_reminder as rr_mod

    # Inline-HTTP orchestrators must re-export the canonical function (not a copy).
    assert pd_mod._owner_headers is _owner_headers, "paper_digest has its own _owner_headers"
    assert rp_mod._owner_headers is _owner_headers, "research_pulse has its own _owner_headers"
    assert rr_mod._owner_headers is _owner_headers, "review_reminder has its own _owner_headers"

    # services_client (used by daily_briefing / deadline_warning / author_alerts)
    # uses the same canonical helper — no private copy.
    assert services_client._owner_headers is helpers_mod._owner_headers, (
        "services_client has its own _owner_headers"
    )

    # Smoke-test that the canonical helper produces correct output.
    config = make_bot_config(BotConfig, telegram_chat_id=9999, jarvis_api_key=SecretStr("secret"))
    headers_with_user = _owner_headers(config, 42)
    assert headers_with_user["X-API-Key"] == "secret"
    assert headers_with_user["X-Owner-User-Id"] == "42"

    headers_no_user = _owner_headers(config, None)
    assert headers_no_user["X-API-Key"] == "secret"
    assert "X-Owner-User-Id" not in headers_no_user
