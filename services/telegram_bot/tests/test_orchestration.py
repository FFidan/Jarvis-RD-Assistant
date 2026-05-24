"""Orchestration workflow tests.

Covers:
- author_alerts: alerts when new papers by tracked authors are found
- daily_briefing: morning briefing message sent to owner
- deadline_warning: milestone deadline warnings within next 3 days
- review_reminder: spaced repetition due-cards reminder
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from jarvis_common.testing import make_bot_config, make_pool_and_conn
from pydantic import SecretStr
from telegram_bot.orchestration import author_alerts as author_alerts_mod
from telegram_bot.orchestration import daily_briefing as daily_briefing_mod
from telegram_bot.orchestration import deadline_warning as deadline_warning_mod
from telegram_bot.orchestration import review_reminder as review_reminder_mod

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _async_cm(return_value):
    """Return a MagicMock that works as an async context manager."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=return_value)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


# ---------------------------------------------------------------------------
# test_author_alerts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_author_alerts_sends_message_when_new_paper_found():
    """run_author_alerts sends an HTML alert when a tracked author has a new paper."""
    bot = AsyncMock()
    http_client = AsyncMock(spec=httpx.AsyncClient)
    config = make_bot_config(telegram_chat_id=9999, jarvis_api_key=SecretStr("secret"))

    # Build a paper that matches the tracked author by name
    paper_id = 42
    tracked_name = "Alice Smith"
    author_row = MagicMock()
    author_row.__getitem__ = lambda _self, k: {
        "id": 1,
        "author_name": tracked_name,
        "s2_author_id": None,
        "enabled": True,
    }[k]
    author_row.get = lambda k, d=None: {
        "id": 1,
        "author_name": tracked_name,
        "s2_author_id": None,
        "enabled": True,
    }.get(k, d)

    paper_row = MagicMock()
    paper_row.__getitem__ = lambda _self, k: {
        "id": paper_id,
        "title": "Paper by Alice",
        "authors": [tracked_name],
        "abstract": "Test abstract",
        "url": "https://example.com/paper",
        "source_type": "arxiv",
        "published_date": date.today(),
        "metadata": {},
    }[k]
    paper_row.get = lambda k, d=None: {
        "id": paper_id,
        "title": "Paper by Alice",
        "authors": [tracked_name],
        "abstract": "Test abstract",
        "url": "https://example.com/paper",
        "source_type": "arxiv",
        "published_date": date.today(),
        "metadata": {},
    }.get(k, d)

    # First acquire: fetch recent papers (shared across all users)
    conn_papers = AsyncMock()
    conn_papers.fetch.return_value = [paper_row]

    # Second acquire (per-pairing): fetch tracked authors scoped to user_id
    conn_authors = AsyncMock()
    conn_authors.fetch.return_value = [author_row]

    # Third acquire (per-author): dedup insert + last_checked_at update
    conn_write = AsyncMock()
    # INSERT … ON CONFLICT … RETURNING → returns a row (new alert)
    insert_row = MagicMock()
    insert_row.__getitem__ = lambda _self, k: {"tracked_author_id": 1}[k]
    conn_write.fetchrow.return_value = insert_row
    conn_write.execute.return_value = None

    pool = MagicMock()
    call_count = 0

    def _acquire():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _async_cm(conn_papers)
        if call_count == 2:
            return _async_cm(conn_authors)
        return _async_cm(conn_write)

    pool.acquire.side_effect = _acquire

    from telegram_bot.owner import UserPairing

    with patch(
        "telegram_bot.owner.list_user_pairings",
        AsyncMock(return_value=[UserPairing(user_id=1, chat_id=9999)]),
    ):
        await author_alerts_mod.run_author_alerts(http_client, pool, bot, config)

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
            make_bot_config(telegram_chat_id=9999, jarvis_api_key=SecretStr("secret")),
        )

    bot.send_message.assert_not_awaited()


# ---------------------------------------------------------------------------
# test_daily_briefing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_daily_briefing_sends_briefing_with_two_papers():
    """run_daily_briefing sends an HTML morning briefing message that includes paper count."""
    bot = AsyncMock()
    config = make_bot_config(telegram_chat_id=9999, jarvis_api_key=SecretStr("secret"))

    now = datetime.now(UTC)
    conn = AsyncMock()
    # new_papers_count query
    count_row = MagicMock()
    count_row.__getitem__ = lambda _self, k: {"count": 2}[k]

    task_row = MagicMock()
    task_row.__getitem__ = lambda _self, k: {"title": "Write paper", "project_name": "ResearchX"}[k]
    task_row.get = lambda k, d=None: {"title": "Write paper", "project_name": "ResearchX"}.get(k, d)

    milestone_row = MagicMock()
    milestone_row.__getitem__ = lambda _self, k: {
        "name": "Submit draft",
        "deadline": now + timedelta(days=3),
        "project_name": "ResearchX",
    }[k]
    milestone_row.get = lambda k, d=None: {
        "name": "Submit draft",
        "deadline": now + timedelta(days=3),
        "project_name": "ResearchX",
    }.get(k, d)

    conn.fetchrow.return_value = count_row
    conn.fetch.side_effect = [
        [task_row],  # in-progress tasks
        [milestone_row],  # upcoming milestones
    ]

    pool, _ = make_pool_and_conn(conn=conn)

    # Mock the learning engine /api/stats call
    http_client = AsyncMock(spec=httpx.AsyncClient)
    stats_resp = MagicMock()
    stats_resp.raise_for_status.return_value = None
    stats_resp.json.return_value = {"due_now": 5}
    http_client.get.return_value = stats_resp

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
async def test_daily_briefing_stats_call_includes_api_key_header():
    """DOM-D-04: _run_briefing_for_chat passes X-API-Key header on /api/stats GET."""
    bot = AsyncMock()
    config = make_bot_config(telegram_chat_id=9999, jarvis_api_key=SecretStr("secret"))

    conn = AsyncMock()
    count_row = MagicMock()
    count_row.__getitem__ = lambda _self, k: {"count": 0}[k]
    conn.fetchrow.return_value = count_row
    conn.fetch.side_effect = [[], []]  # tasks, milestones

    pool, _ = make_pool_and_conn(conn=conn)

    http_client = AsyncMock(spec=httpx.AsyncClient)
    stats_resp = MagicMock()
    stats_resp.raise_for_status.return_value = None
    stats_resp.json.return_value = {"due_now": 1}
    http_client.get.return_value = stats_resp

    from telegram_bot.owner import UserPairing

    with patch(
        "telegram_bot.owner.list_user_pairings",
        AsyncMock(return_value=[UserPairing(user_id=1, chat_id=9999)]),
    ):
        await daily_briefing_mod.run_daily_briefing(http_client, pool, bot, config)

    http_client.get.assert_awaited_once()
    _, call_kwargs = http_client.get.await_args
    assert call_kwargs["headers"].get("X-API-Key") == "secret"


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
            make_bot_config(telegram_chat_id=9999, jarvis_api_key=SecretStr("secret")),
        )

    bot.send_message.assert_not_awaited()


# ---------------------------------------------------------------------------
# test_deadline_warning
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deadline_warning_sends_alert_for_upcoming_milestones():
    """run_deadline_warning sends a warning message listing imminent milestones."""
    bot = AsyncMock()
    http_client = AsyncMock(spec=httpx.AsyncClient)
    config = make_bot_config(telegram_chat_id=9999, jarvis_api_key=SecretStr("secret"))

    now = datetime.now(UTC)
    milestone_row = MagicMock()
    milestone_row.__getitem__ = lambda _self, k: {
        "name": "Grant submission",
        "deadline": now + timedelta(days=1),
        "project_name": "FundingProject",
    }[k]
    milestone_row.get = lambda k, d=None: {
        "name": "Grant submission",
        "deadline": now + timedelta(days=1),
        "project_name": "FundingProject",
    }.get(k, d)

    pool = AsyncMock()
    pool.fetch.return_value = [milestone_row]

    from telegram_bot.owner import UserPairing

    with patch(
        "telegram_bot.owner.list_user_pairings",
        AsyncMock(return_value=[UserPairing(user_id=1, chat_id=9999)]),
    ):
        await deadline_warning_mod.run_deadline_warning(http_client, pool, bot, config)

    bot.send_message.assert_awaited_once()
    _, kwargs = bot.send_message.await_args
    assert kwargs["chat_id"] == 9999
    assert kwargs["parse_mode"] == "HTML"
    assert "Grant submission" in kwargs["text"]


@pytest.mark.asyncio
async def test_deadline_warning_silent_when_no_milestones():
    """run_deadline_warning sends nothing when pairings exist but no milestones are due."""
    bot = AsyncMock()
    http_client = AsyncMock(spec=httpx.AsyncClient)
    pool = AsyncMock()
    pool.fetch.return_value = []

    from telegram_bot.owner import UserPairing

    with patch(
        "telegram_bot.owner.list_user_pairings",
        AsyncMock(return_value=[UserPairing(user_id=1, chat_id=9999)]),
    ):
        await deadline_warning_mod.run_deadline_warning(
            http_client,
            pool,
            bot,
            make_bot_config(telegram_chat_id=9999, jarvis_api_key=SecretStr("secret")),
        )

    bot.send_message.assert_not_awaited()


# ---------------------------------------------------------------------------
# test_review_reminder
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_review_reminder_sends_message_for_due_cards():
    """run_review_reminder sends an inline-button message when cards are due."""
    bot = AsyncMock()
    config = make_bot_config(telegram_chat_id=9999, jarvis_api_key=SecretStr("secret"))
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
            make_bot_config(telegram_chat_id=9999, jarvis_api_key=SecretStr("secret")),
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
            make_bot_config(telegram_chat_id=9999, jarvis_api_key=SecretStr("secret")),
        )

    bot.send_message.assert_not_awaited()


# ---------------------------------------------------------------------------
# test__owner_headers consolidation
# ---------------------------------------------------------------------------


def test_owner_headers_all_orchestrators_use_canonical():
    """Verify the 4 orchestration modules import _owner_headers from helpers
    rather than defining their own copy.

    This is a structural test: it checks that the canonical helper is reachable
    from each module and that calling it produces the expected headers.
    """

    from telegram_bot.handlers.helpers import _owner_headers
    from telegram_bot.orchestration import daily_briefing as db_mod
    from telegram_bot.orchestration import paper_digest as pd_mod
    from telegram_bot.orchestration import research_pulse as rp_mod
    from telegram_bot.orchestration import review_reminder as rr_mod

    # Each module must re-export the canonical function (not a private copy).
    assert db_mod._owner_headers is _owner_headers, "daily_briefing has its own _owner_headers"
    assert pd_mod._owner_headers is _owner_headers, "paper_digest has its own _owner_headers"
    assert rp_mod._owner_headers is _owner_headers, "research_pulse has its own _owner_headers"
    assert rr_mod._owner_headers is _owner_headers, "review_reminder has its own _owner_headers"

    # Smoke-test that the canonical helper produces correct output.
    config = make_bot_config(telegram_chat_id=9999, jarvis_api_key=SecretStr("secret"))
    headers_with_user = _owner_headers(config, 42)
    assert headers_with_user["X-API-Key"] == "secret"
    assert headers_with_user["X-Owner-User-Id"] == "42"

    headers_no_user = _owner_headers(config, None)
    assert headers_no_user["X-API-Key"] == "secret"
    assert "X-Owner-User-Id" not in headers_no_user
