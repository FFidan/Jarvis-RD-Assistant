"""Focused tests for Telegram project/task row rendering shapes."""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jarvis_common.testing import PTBContextOptions, make_bot_config, make_ptb_context
from jarvis_common.testing_telegram import make_http_response
from telegram_bot.config import BotConfig
from telegram_bot.formatters import format_project_status
from telegram_bot.handlers.commands.project_commands import projects_command
from telegram_bot.handlers.commands.task_commands import tasks_command
from telegram_bot.vocabulary import PROJECT_STATUS_LABELS

_TEST_CHAT_ID = 12345

pytestmark = pytest.mark.usefixtures("_clear_rate_limit_state")


def _make_update_and_context(args=None):
    update = MagicMock()
    update.effective_chat = MagicMock()
    update.effective_chat.id = _TEST_CHAT_ID
    update.message = MagicMock()
    update.message.reply_text = AsyncMock()

    http = AsyncMock()
    context = make_ptb_context(
        AsyncMock(),
        make_bot_config(BotConfig),
        options=PTBContextOptions(
            http_client=http, args=args or [], user_data={"jarvis_user_id": 1}
        ),
    )
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


def test_project_detail_states_its_label_counts_and_not_done_tasks() -> None:
    """Project detail reads the shared label and both linked counts, not the raw status."""
    project = {
        "name": "Project Alpha",
        "status": "paused",
        "description": "A research project",
        "paper_count": 12,
        "open_question_count": 3,
    }
    tasks = [
        {"title": "Draft outline", "status": "todo"},
        {"title": "Run pilot", "status": "blocked"},
        {"title": "Submit", "status": "done"},
    ]

    text = format_project_status(project, tasks, milestones=[])

    assert "Draft" in text and "paused" not in text
    assert "Tasks: 1 of 3 done" in text
    assert "Linked papers: 12" in text
    assert "Open questions: 3" in text
    # todo and blocked both count as open, matching My Day's not-done rule.
    assert "Open tasks (2)" in text
    assert "Submit" not in text


# ---------------------------------------------------------------------------
# Project-status vocabulary: the bot's map and the web app's must stay equal
# ---------------------------------------------------------------------------

_LABELS_TS = (
    Path(__file__).resolve().parents[3] / "frontend" / "src" / "lib" / "labels" / "projectStatus.ts"
)
_TS_MAP_RE = re.compile(r"PROJECT_STATUS_LABELS[^=]*=\s*\{(?P<body>[^}]*)\}", re.DOTALL)
_TS_ENTRY_RE = re.compile(r"(?P<key>\w+)\s*:\s*'(?P<label>[^']*)'")


def _typescript_status_labels() -> dict[str, str]:
    """Read the web app's project-status labels out of its TypeScript source."""
    match = _TS_MAP_RE.search(_LABELS_TS.read_text(encoding="utf-8"))
    assert match is not None, f"PROJECT_STATUS_LABELS literal not found in {_LABELS_TS.name}"
    entries = dict(
        (m.group("key"), m.group("label")) for m in _TS_ENTRY_RE.finditer(match.group("body"))
    )
    assert entries, f"PROJECT_STATUS_LABELS in {_LABELS_TS.name} parsed as empty"
    return entries


def test_bot_project_status_labels_equal_the_web_app_labels() -> None:
    """The bot's status vocabulary is the web app's, key for key and label for label.

    The vocabulary has to cross a language boundary, so it exists twice; this
    pins the copies equal. Renaming a status, dropping one, or rewording a
    label on either side alone fails here.
    """
    assert PROJECT_STATUS_LABELS == _typescript_status_labels()
