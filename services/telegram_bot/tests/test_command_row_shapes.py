"""Focused tests for Telegram project/task row rendering shapes."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import SecretStr
from telegram_bot.config import BotConfig
from telegram_bot.handlers import rate_limit as _rate_limit_mod
from telegram_bot.handlers.commands.project_commands import projects_command
from telegram_bot.handlers.commands.task_commands import tasks_command

_TEST_CHAT_ID = 12345


def _make_config() -> BotConfig:
    return BotConfig(
        telegram_token="test-token",
        telegram_chat_id=_TEST_CHAT_ID,
        database_url="postgres://test",
        paper_ingestion_url="http://paper:8000",
        learning_engine_url="http://learn:8001",
        jarvis_api_key=SecretStr("test-key"),
    )


def _make_update_and_context(args=None):
    update = MagicMock()
    update.effective_chat = MagicMock()
    update.effective_chat.id = _TEST_CHAT_ID
    update.message = MagicMock()
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.args = args or []
    db = AsyncMock()
    context.application = MagicMock()
    context.application.bot_data = {
        "config": _make_config(),
        "db_pool": db,
        "http_client": AsyncMock(),
    }
    return update, context, db


@pytest.fixture(autouse=True)
def _clear_rate_limit_state():
    """Command decorators share rate-limit memory across tests."""
    _rate_limit_mod._timestamps.clear()
    yield
    _rate_limit_mod._timestamps.clear()


@pytest.mark.asyncio
async def test_projects_command_renders_explicit_project_row_fields() -> None:
    """Project list rendering should use the selected project row fields."""
    update, context, db = _make_update_and_context()
    db.fetch.return_value = [
        {
            "id": 42,
            "name": "Project <Alpha>",
            "status": "active",
            "description": "Important <work>",
            "deadline": None,
        }
    ]

    await projects_command(update, context)

    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.await_args.args[0]
    assert "Project &lt;Alpha&gt;" in text
    assert "Important &lt;work&gt;" in text
    assert (
        update.message.reply_text.await_args.kwargs["reply_markup"]
        .inline_keyboard[0][0]
        .callback_data
        == "project_detail_42"
    )


@pytest.mark.asyncio
async def test_tasks_command_renders_joined_project_name_when_present() -> None:
    """Task list rendering should include the LEFT JOIN project name when supplied."""
    update, context, db = _make_update_and_context()
    db.fetch.return_value = [
        {
            "id": 7,
            "title": "Write <tests>",
            "status": "in_progress",
            "project_name": "Cleanup <Wave>",
        }
    ]

    await tasks_command(update, context)

    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.await_args.args[0]
    assert "[7] Write &lt;tests&gt;" in text
    assert "(Cleanup &lt;Wave&gt;)" in text
