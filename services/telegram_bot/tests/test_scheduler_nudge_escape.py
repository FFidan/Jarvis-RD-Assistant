"""Tests for TG-001: HTML injection prevention in scheduler nudge alert messages.

Verifies that user-controlled fields (nudge_type) are escaped through
formatters.escape() before being interpolated into HTML parse_mode messages.

Also covers owner-only delivery of failure alerts (telegram.owner_chat_id path).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram_bot.scheduler import JarvisScheduler

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_scheduler(owner_chat_id: int | None = 99) -> JarvisScheduler:
    """Return a JarvisScheduler with all deps mocked out.

    db_pool.fetchrow resolves telegram.owner_chat_id (the single chat the
    failure alert is delivered to). Pass owner_chat_id=None to simulate an
    unconfigured owner.
    """
    db_pool = MagicMock()
    db_pool.execute = AsyncMock()
    row = {"value": str(owner_chat_id)} if owner_chat_id is not None else None
    db_pool.fetchrow = AsyncMock(return_value=row)
    http_client = MagicMock()
    bot = MagicMock()
    bot.send_message = AsyncMock()
    config = MagicMock()
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

    with patch.dict("telegram_bot.scheduler.JOB_REGISTRY", {}, clear=True):
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
        await scheduler._run_job(safe_nudge_type, nudge_id)

    call_kwargs = scheduler.bot.send_message.call_args.kwargs
    text: str = call_kwargs["text"]

    assert safe_nudge_type in text


@pytest.mark.asyncio
async def test_no_alert_sent_when_owner_chat_unconfigured() -> None:
    """With no telegram.owner_chat_id configured, no failure alert is sent."""
    scheduler = _make_scheduler(owner_chat_id=None)
    with patch.dict("telegram_bot.scheduler.JOB_REGISTRY", {}, clear=True):
        await scheduler._run_job("daily_summary", 5)
    scheduler.bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_failure_alert_sent_only_to_owner_chat() -> None:
    """Failure alert goes to telegram.owner_chat_id only — never broadcast."""
    scheduler = _make_scheduler(owner_chat_id=4242)
    with patch.dict("telegram_bot.scheduler.JOB_REGISTRY", {}, clear=True):
        await scheduler._run_job("daily_summary", 5)
    scheduler.bot.send_message.assert_called_once()
    assert scheduler.bot.send_message.call_args.kwargs["chat_id"] == 4242


@pytest.mark.asyncio
async def test_run_job_skips_under_maintenance_sentinel(tmp_path, monkeypatch) -> None:
    """A fresh maintenance sentinel makes _run_job return before the last_fired_at UPDATE."""
    monkeypatch.setenv("MAINTENANCE_SENTINEL", str(tmp_path / ".maintenance"))
    monkeypatch.setenv("MAINTENANCE_DESTRUCTIVE_SENTINEL", str(tmp_path / ".destructive"))
    (tmp_path / ".maintenance").touch()

    scheduler = _make_scheduler()
    await scheduler._run_job("daily_summary", 5)

    scheduler.db_pool.execute.assert_not_called()  # no UPDATE scheduled_nudges
    scheduler.bot.send_message.assert_not_called()  # not treated as a failure
