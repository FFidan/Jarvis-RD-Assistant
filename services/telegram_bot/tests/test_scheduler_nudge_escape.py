"""Tests for TG-001: HTML injection prevention in scheduler nudge alert messages.

Verifies that user-controlled fields (nudge_type) are escaped through
formatters.escape() before being interpolated into HTML parse_mode messages.

Also covers per-pairing delivery of failure alerts (list_user_pairings path).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram_bot.owner import UserPairing
from telegram_bot.scheduler import JarvisScheduler

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_scheduler() -> JarvisScheduler:
    """Return a JarvisScheduler with all deps mocked out."""
    db_pool = MagicMock()
    db_pool.execute = AsyncMock()
    http_client = MagicMock()
    bot = MagicMock()
    bot.send_message = AsyncMock()
    config = MagicMock()
    return JarvisScheduler(db_pool=db_pool, http_client=http_client, bot=bot, config=config)


def _one_pairing(chat_id: int = 99) -> list[UserPairing]:
    return [UserPairing(user_id=1, chat_id=chat_id)]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nudge_type_html_injection_is_escaped() -> None:
    """nudge_type containing HTML tags must be escaped in the failure alert."""
    scheduler = _make_scheduler()
    malicious_nudge_type = "<script>alert(1)</script>"
    nudge_id = 7

    with patch.dict("telegram_bot.scheduler.JOB_REGISTRY", {}, clear=True):
        with patch(
            "telegram_bot.owner.list_user_pairings", new=AsyncMock(return_value=_one_pairing(99))
        ):
            await scheduler._run_job(malicious_nudge_type, nudge_id)

    scheduler.bot.send_message.assert_called_once()
    call_kwargs = scheduler.bot.send_message.call_args.kwargs
    text: str = call_kwargs["text"]

    assert "<script>" not in text, "Raw <script> tag leaked into Telegram HTML message"
    assert "</script>" not in text, "Raw </script> tag leaked into Telegram HTML message"
    assert "&lt;script&gt;" in text, "Escaped &lt;script&gt; not found in alert text"
    assert "&lt;/script&gt;" in text, "Escaped &lt;/script&gt; not found in alert text"
    assert call_kwargs.get("parse_mode") == "HTML"


@pytest.mark.asyncio
async def test_nudge_type_ampersand_is_escaped() -> None:
    """nudge_type containing & must be escaped as &amp; in the failure alert."""
    scheduler = _make_scheduler()
    nudge_type_with_amp = "foo&bar"
    nudge_id = 3

    with patch.dict("telegram_bot.scheduler.JOB_REGISTRY", {}, clear=True):
        with patch(
            "telegram_bot.owner.list_user_pairings", new=AsyncMock(return_value=_one_pairing(99))
        ):
            await scheduler._run_job(nudge_type_with_amp, nudge_id)

    call_kwargs = scheduler.bot.send_message.call_args.kwargs
    text: str = call_kwargs["text"]

    sanitized = text.replace("&amp;", "").replace("&lt;", "").replace("&gt;", "")
    assert "&" not in sanitized, "Unescaped & still present in alert text"
    assert "&amp;" in text


@pytest.mark.asyncio
async def test_safe_nudge_type_passes_through() -> None:
    """A safe nudge_type (no HTML special chars) is rendered unchanged."""
    scheduler = _make_scheduler()
    safe_nudge_type = "daily_summary"
    nudge_id = 1

    with patch.dict("telegram_bot.scheduler.JOB_REGISTRY", {}, clear=True):
        with patch(
            "telegram_bot.owner.list_user_pairings", new=AsyncMock(return_value=_one_pairing(99))
        ):
            await scheduler._run_job(safe_nudge_type, nudge_id)

    call_kwargs = scheduler.bot.send_message.call_args.kwargs
    text: str = call_kwargs["text"]

    assert safe_nudge_type in text


@pytest.mark.asyncio
async def test_no_alert_sent_when_no_pairings() -> None:
    """When list_user_pairings returns empty, no message is sent."""
    scheduler = _make_scheduler()

    with patch.dict("telegram_bot.scheduler.JOB_REGISTRY", {}, clear=True):
        with patch("telegram_bot.owner.list_user_pairings", new=AsyncMock(return_value=[])):
            await scheduler._run_job("daily_summary", 5)

    scheduler.bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_alert_sent_to_each_paired_user() -> None:
    """Failure alert is sent once per pairing when multiple users are paired."""
    scheduler = _make_scheduler()
    pairings = [UserPairing(user_id=1, chat_id=11), UserPairing(user_id=2, chat_id=22)]

    with patch.dict("telegram_bot.scheduler.JOB_REGISTRY", {}, clear=True):
        with patch("telegram_bot.owner.list_user_pairings", new=AsyncMock(return_value=pairings)):
            await scheduler._run_job("daily_summary", 5)

    assert scheduler.bot.send_message.call_count == 2
    sent_chat_ids = {call.kwargs["chat_id"] for call in scheduler.bot.send_message.call_args_list}
    assert sent_chat_ids == {11, 22}
