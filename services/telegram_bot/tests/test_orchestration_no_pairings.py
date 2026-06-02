"""Verify all 6 orchestrations fail-loud (warn + return) when pairings are empty."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.asyncio


@pytest.fixture
def mock_deps():
    """Stock asyncpg.Pool / Bot / httpx.AsyncClient / BotConfig fixtures."""
    db_pool = MagicMock()
    # list_user_pairings (the only db_pool consumer that runs before the guard)
    # is patched in each test, so db_pool.fetch is never reached — but keep it
    # awaitable as a defensive default.
    db_pool.fetch = AsyncMock(return_value=[])
    bot = AsyncMock()
    http_client = AsyncMock()
    config = MagicMock()
    config.jarvis_api_key = None
    config.paper_ingestion_url = "http://paper_ingestion:8000"
    config.learning_engine_url = "http://learning_engine:8001"
    return db_pool, bot, http_client, config


async def _assert_skips_with_warning(run_fn, caplog, mock_deps) -> None:
    db_pool, bot, http_client, config = mock_deps
    # run_* functions call `owner.list_user_pairings(...)` via a module-level
    # `from telegram_bot import owner as _owner`, so patching the attribute on the
    # owner module — the canonical source location — is intercepted at the call site.
    with patch("telegram_bot.owner.list_user_pairings", AsyncMock(return_value=[])):
        caplog.set_level(logging.WARNING)
        await run_fn(http_client=http_client, db_pool=db_pool, bot=bot, config=config)
    # Real invariant: no message delivered when there are no pairings.
    bot.send_message.assert_not_called()
    # Soft check: at least one warning was logged (exact text is implementation detail).
    assert any(r.levelno >= logging.WARNING for r in caplog.records), (
        f"expected at least one WARNING log when no pairings found; got: "
        f"{[(r.levelname, r.message) for r in caplog.records]}"
    )


async def test_paper_digest_skips_with_warning_when_no_pairings(caplog, mock_deps):
    from telegram_bot.orchestration.paper_digest import run_paper_digest

    await _assert_skips_with_warning(run_paper_digest, caplog, mock_deps)


async def test_author_alerts_skips_with_warning_when_no_pairings(caplog, mock_deps):
    from telegram_bot.orchestration.author_alerts import run_author_alerts

    await _assert_skips_with_warning(run_author_alerts, caplog, mock_deps)


async def test_daily_briefing_skips_with_warning_when_no_pairings(caplog, mock_deps):
    from telegram_bot.orchestration.daily_briefing import run_daily_briefing

    await _assert_skips_with_warning(run_daily_briefing, caplog, mock_deps)


async def test_review_reminder_skips_with_warning_when_no_pairings(caplog, mock_deps):
    from telegram_bot.orchestration.review_reminder import run_review_reminder

    await _assert_skips_with_warning(run_review_reminder, caplog, mock_deps)


async def test_research_pulse_skips_with_warning_when_no_pairings(caplog, mock_deps):
    from telegram_bot.orchestration.research_pulse import run_research_pulse

    await _assert_skips_with_warning(run_research_pulse, caplog, mock_deps)


async def test_deadline_warning_skips_with_warning_when_no_pairings(caplog, mock_deps):
    """deadline_warning guards on pairings FIRST and warns + returns when empty.

    Milestones are now fetched per-pairing via services_client (REST), so the
    pairings guard runs before any milestone lookup; the generic helper covers
    this case without special milestone setup.
    """
    from telegram_bot.orchestration.deadline_warning import run_deadline_warning

    await _assert_skips_with_warning(run_deadline_warning, caplog, mock_deps)
