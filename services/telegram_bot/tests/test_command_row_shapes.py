"""Focused tests for Telegram project/task row rendering shapes."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jarvis_common.testing import make_bot_config
from jarvis_common.testing_telegram import make_http_response
from telegram_bot.config import BotConfig
from telegram_bot.handlers.commands.project_commands import projects_command
from telegram_bot.handlers.commands.task_commands import tasks_command

_TEST_CHAT_ID = 12345

pytestmark = pytest.mark.usefixtures("_clear_rate_limit_state")


def _make_update_and_context(args=None):
    update = MagicMock()
    update.effective_chat = MagicMock()
    update.effective_chat.id = _TEST_CHAT_ID
    update.message = MagicMock()
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.args = args or []
    context.user_data = {"jarvis_user_id": 1}
    http = AsyncMock()
    context.application = MagicMock()
    context.application.bot_data = {
        "config": make_bot_config(BotConfig, telegram_chat_id=_TEST_CHAT_ID),
        "db_pool": AsyncMock(),
        "http_client": http,
    }
    return update, context, http


@pytest.fixture(autouse=True)
def _default_auth_patch():
    """Paired user auth for all tests in this module (multi-user mode requires pairing)."""
    with patch(
        "telegram_bot.handlers.commands._auth.auth_check",
        new_callable=AsyncMock,
        return_value=(True, 1),
    ):
        yield


@pytest.mark.asyncio
async def test_projects_command_renders_explicit_project_row_fields() -> None:
    """Project list rendering should use the REST project row fields."""
    update, context, http = _make_update_and_context()
    http.get.return_value = make_http_response(
        [
            {
                "id": 42,
                "name": "Project <Alpha>",
                "status": "active",
                "description": "Important <work>",
                "deadline": None,
            }
        ]
    )

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
    """Task list rendering should include the project_name field when supplied."""
    update, context, http = _make_update_and_context()
    http.get.return_value = make_http_response(
        [
            {
                "id": 7,
                "title": "Write <tests>",
                "status": "in_progress",
                "project_name": "Cleanup <Wave>",
            }
        ]
    )

    await tasks_command(update, context)

    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.await_args.args[0]
    assert "[7] Write &lt;tests&gt;" in text
    assert "(Cleanup &lt;Wave&gt;)" in text
