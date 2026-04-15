"""Tests for Telegram bot inline-keyboard callback handlers.

Covers: paper_detail, paper_bookmark, project_detail, task_done, start_review.
Each handler is tested directly with mocked Update + Context objects.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure the telegram_bot app package is importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Stub heavy native modules unavailable outside Docker.
for _mod_name in (
    "telegram",
    "telegram.ext",
    "apscheduler",
    "apscheduler.schedulers",
    "apscheduler.schedulers.asyncio",
    "apscheduler.triggers",
    "apscheduler.triggers.cron",
):
    if _mod_name not in sys.modules:
        sys.modules[_mod_name] = MagicMock()

_tg = sys.modules["telegram"]
_tg.Update = MagicMock
_tg.InlineKeyboardButton = lambda *a, **kw: MagicMock()
_tg.InlineKeyboardMarkup = lambda *a, **kw: MagicMock()

# Ensure Message and BotCommand stubs are set (conftest may already set them, but guard here too).
if not isinstance(getattr(_tg, "Message", None), type):

    class _FakeMessage:
        """Minimal stub for telegram.Message."""

    _tg.Message = _FakeMessage

if not callable(getattr(_tg, "BotCommand", None)):
    _tg.BotCommand = lambda cmd, desc: (cmd, desc)

_FakeMessage = _tg.Message  # local alias for use in helpers below

_tg_ext = sys.modules["telegram.ext"]
_tg_ext.Application = MagicMock
_tg_ext.CommandHandler = MagicMock
_tg_ext.CallbackQueryHandler = MagicMock
_tg_ext.ContextTypes = MagicMock()
_tg_ext.ContextTypes.DEFAULT_TYPE = MagicMock
_tg_ext.ConversationHandler = MagicMock()
_tg_ext.ConversationHandler.END = -1

from app.config import BotConfig  # noqa: E402
from app.handlers.callback_handler import (  # noqa: E402
    paper_bookmark_callback,
    paper_detail_callback,
    project_detail_callback,
    start_review_callback,
    task_done_callback,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TEST_CHAT_ID = 12345


def _make_config() -> BotConfig:
    return BotConfig(
        telegram_token="test-token",
        telegram_chat_id=_TEST_CHAT_ID,
        database_url="postgres://test",
        paper_ingestion_url="http://paper:8000",
        learning_engine_url="http://learn:8001",
        jarvis_api_key="test-key",
    )


def _make_callback_update_and_context(callback_data: str, chat_id=_TEST_CHAT_ID):
    """Build mock Update + Context for callback query handlers."""
    update = MagicMock()
    update.effective_chat = MagicMock()
    update.effective_chat.id = chat_id

    query = MagicMock()
    query.data = callback_data
    query.answer = AsyncMock()
    # Use _FakeMessage so isinstance(query.message, Message) passes in handlers.
    fake_msg = _FakeMessage()
    fake_msg.reply_text = AsyncMock()  # type: ignore[attr-defined]
    query.message = fake_msg
    query.edit_message_text = AsyncMock()
    update.callback_query = query

    context = MagicMock()
    config = _make_config()
    mock_db = AsyncMock()
    mock_http = AsyncMock()

    context.application = MagicMock()
    context.application.bot_data = {
        "config": config,
        "db_pool": mock_db,
        "http_client": mock_http,
    }

    return update, context, mock_db, mock_http


# ---------------------------------------------------------------------------
# Tests: paper_detail
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_paper_detail_success():
    """paper_detail callback fetches and displays paper details."""
    update, context, _, mock_http = _make_callback_update_and_context("paper_detail_42")
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {
        "paper": {
            "title": "Great Paper",
            "authors": ["Author A"],
            "published_date": "2025-01-01",
            "url": "http://example.com",
        },
        "summary": None,
    }
    mock_http.get.return_value = mock_resp

    await paper_detail_callback(update, context)

    update.callback_query.answer.assert_awaited_once()
    update.callback_query.message.reply_text.assert_awaited_once()
    text = update.callback_query.message.reply_text.call_args[0][0]
    assert "Great Paper" in text


@pytest.mark.asyncio
async def test_paper_detail_api_failure():
    """paper_detail callback sends error when API fails."""
    update, context, _, mock_http = _make_callback_update_and_context("paper_detail_42")
    mock_http.get.side_effect = Exception("Connection refused")

    await paper_detail_callback(update, context)

    update.callback_query.answer.assert_awaited_once()
    update.callback_query.message.reply_text.assert_awaited_once()
    text = update.callback_query.message.reply_text.call_args[0][0]
    assert "Failed" in text or "failed" in text.lower()


# ---------------------------------------------------------------------------
# Tests: paper_bookmark
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_paper_bookmark_success():
    """paper_bookmark callback bookmarks a paper."""
    update, context, mock_db, _ = _make_callback_update_and_context("paper_bookmark_7")

    await paper_bookmark_callback(update, context)

    update.callback_query.answer.assert_awaited_once()
    mock_db.execute.assert_awaited_once()
    text = update.callback_query.message.reply_text.call_args[0][0]
    assert "bookmarked" in text.lower() or "7" in text


# ---------------------------------------------------------------------------
# Tests: project_detail
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_project_detail_success():
    """project_detail callback shows project status."""
    update, context, mock_db, _ = _make_callback_update_and_context("project_detail_3")
    mock_db.fetchrow.return_value = {
        "id": 3,
        "name": "My Project",
        "status": "active",
        "description": "A project",
        "deadline": None,
    }
    mock_db.fetch.side_effect = [
        [{"id": 1, "title": "Task A", "status": "in_progress"}],  # tasks
        [{"id": 1, "name": "Milestone 1", "deadline": None, "completed": False}],  # milestones
    ]

    await project_detail_callback(update, context)

    update.callback_query.answer.assert_awaited_once()
    update.callback_query.message.reply_text.assert_awaited_once()
    text = update.callback_query.message.reply_text.call_args[0][0]
    assert "My Project" in text


@pytest.mark.asyncio
async def test_project_detail_not_found():
    """project_detail callback sends 'not found' when project does not exist."""
    update, context, mock_db, _ = _make_callback_update_and_context("project_detail_999")
    mock_db.fetchrow.return_value = None

    await project_detail_callback(update, context)

    update.callback_query.answer.assert_awaited_once()
    text = update.callback_query.message.reply_text.call_args[0][0]
    assert "not found" in text.lower()


# ---------------------------------------------------------------------------
# Tests: task_done
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_done_success():
    """task_done callback marks a task as done."""
    update, context, mock_db, _ = _make_callback_update_and_context("task_done_10")

    with patch("app.handlers.callback_handler.ProjectManager") as mock_pm:
        pm_instance = AsyncMock()
        pm_instance.complete_task.return_value = {"id": 10, "status": "done"}
        mock_pm.return_value = pm_instance

        await task_done_callback(update, context)

    update.callback_query.answer.assert_awaited_once()
    text = update.callback_query.message.reply_text.call_args[0][0]
    assert "done" in text.lower() or "10" in text


@pytest.mark.asyncio
async def test_task_done_not_found():
    """task_done callback sends 'not found' when task does not exist."""
    update, context, mock_db, _ = _make_callback_update_and_context("task_done_999")

    with patch("app.handlers.callback_handler.ProjectManager") as mock_pm:
        pm_instance = AsyncMock()
        pm_instance.complete_task.return_value = {}
        mock_pm.return_value = pm_instance

        await task_done_callback(update, context)

    text = update.callback_query.message.reply_text.call_args[0][0]
    assert "not found" in text.lower() or "999" in text


# ---------------------------------------------------------------------------
# Tests: start_review
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_review_callback_delegates_to_review_start():
    """start_review callback delegates to review_start rather than printing a stub message."""
    update, context, _, _ = _make_callback_update_and_context("start_review")

    with patch(
        "app.handlers.callback_handler.review_start", new_callable=AsyncMock
    ) as mock_review_start:
        await start_review_callback(update, context)

    update.callback_query.answer.assert_awaited_once()
    mock_review_start.assert_awaited_once()
    # Confirm review_start was called with the same update and context
    called_update, called_context = mock_review_start.call_args[0]
    assert called_update is update
    assert called_context is context
