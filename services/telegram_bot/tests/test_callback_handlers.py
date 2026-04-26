"""Tests for Telegram bot inline-keyboard callback handlers.

Covers: paper_detail, paper_bookmark, project_detail, task_done, start_review.
Each handler is tested directly with mocked Update + Context objects.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import telegram
from telegram_bot.config import BotConfig
from telegram_bot.handlers.callback_handler import (  # noqa: E402
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
    # Use spec=telegram.Message so isinstance(query.message, Message) passes in handlers.
    fake_msg = MagicMock(spec=telegram.Message)
    fake_msg.reply_text = AsyncMock()
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
    """paper_bookmark callback bookmarks a paper via the HTTP endpoint."""
    update, context, _mock_db, mock_http = _make_callback_update_and_context("paper_bookmark_7")
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_http.put.return_value = mock_resp

    await paper_bookmark_callback(update, context)

    update.callback_query.answer.assert_awaited_once()
    mock_http.put.assert_awaited_once()
    call_args = mock_http.put.await_args
    assert "/api/papers/7/bookmark" in call_args[0][0]
    text = update.callback_query.message.reply_text.call_args[0][0]
    assert "bookmarked" in text.lower() or "7" in text


@pytest.mark.asyncio
async def test_paper_bookmark_callback_calls_http_endpoint():
    """paper_bookmark_callback PUTs to the bookmark endpoint with correct paper_id and X-API-Key."""
    update, context, _mock_db, mock_http = _make_callback_update_and_context("paper_bookmark_42")
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_http.put.return_value = mock_resp

    await paper_bookmark_callback(update, context)

    mock_http.put.assert_awaited_once()
    call_args = mock_http.put.await_args
    url = call_args[0][0]
    headers = call_args[1]["headers"]
    assert "/api/papers/42/bookmark" in url
    assert headers.get("X-API-Key") == "test-key"


@pytest.mark.asyncio
async def test_paper_bookmark_callback_handles_api_failure():
    """paper_bookmark_callback sends an error message when the HTTP call fails."""
    update, context, _mock_db, mock_http = _make_callback_update_and_context("paper_bookmark_99")
    mock_http.put.side_effect = Exception("Connection refused")

    await paper_bookmark_callback(update, context)

    update.callback_query.answer.assert_awaited_once()
    text = update.callback_query.message.reply_text.call_args[0][0]
    assert "failed" in text.lower() or "bookmark" in text.lower()


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

    with patch("telegram_bot.handlers.callback_handler.ProjectManager") as mock_pm:
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

    with patch("telegram_bot.handlers.callback_handler.ProjectManager") as mock_pm:
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
        "telegram_bot.handlers.callback_handler.review_start", new_callable=AsyncMock
    ) as mock_review_start:
        await start_review_callback(update, context)

    update.callback_query.answer.assert_awaited_once()
    mock_review_start.assert_awaited_once()
    # Confirm review_start was called with the same update and context
    called_update, called_context = mock_review_start.call_args[0]
    assert called_update is update
    assert called_context is context


# ---------------------------------------------------------------------------
# TG-003: start_review must NOT be registered in register_callback_handlers
# ---------------------------------------------------------------------------


def test_start_review_not_registered_in_callback_handler():
    """TG-003: register_callback_handlers must NOT add a start_review handler.

    The pattern ^start_review$ is exclusively owned by the ConversationHandler
    entry_point in review_handler.py.  A duplicate registration via
    register_callback_handlers causes ghost double-dispatch callbacks.
    """
    from unittest.mock import MagicMock

    from telegram_bot.handlers.callback_handler import register_callback_handlers

    mock_app = MagicMock()
    register_callback_handlers(mock_app)

    # Collect all patterns used in add_handler calls
    registered_patterns = []
    for c in mock_app.add_handler.call_args_list:
        handler_arg = c[0][0] if c[0] else None
        if handler_arg is not None and hasattr(handler_arg, "pattern"):
            registered_patterns.append(str(handler_arg.pattern))

    assert not any("start_review" in p for p in registered_patterns), (
        f"start_review should NOT be registered via register_callback_handlers "
        f"(ConversationHandler owns it). Found patterns: {registered_patterns}"
    )
