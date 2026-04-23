"""Tests for TG-001: HTML injection prevention in scheduler nudge alert messages.

Verifies that user-controlled fields (nudge_type) are escaped through
formatters.escape() before being interpolated into HTML parse_mode messages.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
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
    config.telegram_chat_id = 42
    return JarvisScheduler(db_pool=db_pool, http_client=http_client, bot=bot, config=config)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nudge_type_html_injection_is_escaped() -> None:
    """nudge_type containing HTML tags must be escaped in the failure alert."""
    scheduler = _make_scheduler()
    malicious_nudge_type = "<script>alert(1)</script>"
    nudge_id = 7

    # Patch JOB_REGISTRY so the lookup raises KeyError, triggering the alert path.
    with patch.dict("telegram_bot.scheduler.JOB_REGISTRY", {}, clear=True):
        # Also patch resolve_owner_chat_id so we get past the owner check.
        with patch(
            "telegram_bot.scheduler.resolve_owner_chat_id", new=AsyncMock(return_value=99)
        ) as _mock_owner:
            # _run_job will hit KeyError on JOB_REGISTRY lookup → except block fires.
            await scheduler._run_job(malicious_nudge_type, nudge_id)

    # send_message must have been called exactly once.
    scheduler.bot.send_message.assert_called_once()
    call_kwargs = scheduler.bot.send_message.call_args.kwargs
    text: str = call_kwargs["text"]

    # The raw tag must NOT appear verbatim in the alert text.
    assert "<script>" not in text, "Raw <script> tag leaked into Telegram HTML message"
    assert "</script>" not in text, "Raw </script> tag leaked into Telegram HTML message"

    # The escaped form MUST appear.
    assert "&lt;script&gt;" in text, "Escaped &lt;script&gt; not found in alert text"
    assert "&lt;/script&gt;" in text, "Escaped &lt;/script&gt; not found in alert text"

    # parse_mode must be HTML.
    assert call_kwargs.get("parse_mode") == "HTML"


@pytest.mark.asyncio
async def test_nudge_type_ampersand_is_escaped() -> None:
    """nudge_type containing & must be escaped as &amp; in the failure alert."""
    scheduler = _make_scheduler()
    nudge_type_with_amp = "foo&bar"
    nudge_id = 3

    with patch.dict("telegram_bot.scheduler.JOB_REGISTRY", {}, clear=True):
        with patch("telegram_bot.scheduler.resolve_owner_chat_id", new=AsyncMock(return_value=99)):
            await scheduler._run_job(nudge_type_with_amp, nudge_id)

    call_kwargs = scheduler.bot.send_message.call_args.kwargs
    text: str = call_kwargs["text"]

    assert "&" not in text.replace("&amp;", "").replace("&lt;", "").replace("&gt;", ""), (
        "Unescaped & still present in alert text"
    )
    assert "&amp;" in text


@pytest.mark.asyncio
async def test_safe_nudge_type_passes_through() -> None:
    """A safe nudge_type (no HTML special chars) is rendered unchanged."""
    scheduler = _make_scheduler()
    safe_nudge_type = "daily_summary"
    nudge_id = 1

    with patch.dict("telegram_bot.scheduler.JOB_REGISTRY", {}, clear=True):
        with patch("telegram_bot.scheduler.resolve_owner_chat_id", new=AsyncMock(return_value=99)):
            await scheduler._run_job(safe_nudge_type, nudge_id)

    call_kwargs = scheduler.bot.send_message.call_args.kwargs
    text: str = call_kwargs["text"]

    # Safe value must appear literally in the message.
    assert safe_nudge_type in text


@pytest.mark.asyncio
async def test_no_alert_sent_when_no_owner() -> None:
    """When resolve_owner_chat_id returns None, no message is sent."""
    scheduler = _make_scheduler()

    with patch.dict("telegram_bot.scheduler.JOB_REGISTRY", {}, clear=True):
        with patch(
            "telegram_bot.scheduler.resolve_owner_chat_id", new=AsyncMock(return_value=None)
        ):
            await scheduler._run_job("daily_summary", 5)

    scheduler.bot.send_message.assert_not_called()
