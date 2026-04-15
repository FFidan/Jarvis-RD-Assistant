"""Tests for Telegram bot command handlers.

Covers: /start, /help, /papers, /stats, /briefing, /projects, /tasks, /done, /newproject.
Each handler function is tested directly with mocked Update + Context objects.
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

# Now create stubs for specific objects used at import time
_tg = sys.modules["telegram"]
_tg.Update = MagicMock
_tg.InlineKeyboardButton = lambda *a, **kw: MagicMock()
_tg.InlineKeyboardMarkup = lambda *a, **kw: MagicMock()

_tg_ext = sys.modules["telegram.ext"]
_tg_ext.Application = MagicMock
_tg_ext.CommandHandler = MagicMock
_tg_ext.CallbackQueryHandler = MagicMock
_tg_ext.ContextTypes = MagicMock()
_tg_ext.ContextTypes.DEFAULT_TYPE = MagicMock
_tg_ext.ConversationHandler = MagicMock()
_tg_ext.ConversationHandler.END = -1

from app.config import BotConfig  # noqa: E402
from app.handlers.command_handler import (  # noqa: E402
    briefing_command,
    done_command,
    help_command,
    newproject_command,
    papers_command,
    projects_command,
    start_command,
    stats_command,
    tasks_command,
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


def _make_update_and_context(args=None, chat_id=_TEST_CHAT_ID):
    """Build mock Update + Context for command handlers."""
    update = MagicMock()
    update.effective_chat = MagicMock()
    update.effective_chat.id = chat_id
    update.message = MagicMock()
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.args = args or []

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
# Tests: /start
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_command_sends_welcome():
    """/start sends a welcome message."""
    update, context, _, _ = _make_update_and_context()
    await start_command(update, context)
    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert "Welcome" in text


# ---------------------------------------------------------------------------
# Tests: /help
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_help_command_sends_help():
    """/help sends the help text."""
    update, context, _, _ = _make_update_and_context()
    await help_command(update, context)
    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert "/papers" in text
    assert "/review" in text


# ---------------------------------------------------------------------------
# Tests: /papers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_papers_no_args_lists_recent():
    """/papers with no query lists recent papers from DB."""
    update, context, mock_db, _ = _make_update_and_context(args=[])
    mock_db.fetch.return_value = [
        {
            "id": 1,
            "title": "Paper A",
            "authors": ["Author"],
            "published_date": None,
            "source_type": "arxiv",
            "url": "http://example.com",
            "summary_brief": None,
            "tldr": None,
        },
    ]
    await papers_command(update, context)
    update.message.reply_text.assert_awaited()
    text = update.message.reply_text.call_args_list[0][0][0]
    assert "Paper A" in text


@pytest.mark.asyncio
async def test_papers_with_query_searches_api():
    """/papers <query> calls the paper_ingestion search API."""
    update, context, _, mock_http = _make_update_and_context(args=["transformer"])
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = [
        {
            "id": 1,
            "title": "Transformers Paper",
            "authors": ["Author"],
            "published_date": None,
            "source_type": "arxiv",
            "url": "http://example.com",
            "tldr": None,
            "summary_brief": None,
        },
    ]
    mock_http.post.return_value = mock_resp
    await papers_command(update, context)
    mock_http.post.assert_awaited_once()
    update.message.reply_text.assert_awaited()


@pytest.mark.asyncio
async def test_papers_api_failure_sends_error():
    """/papers <query> sends error message when API fails."""
    update, context, _, mock_http = _make_update_and_context(args=["test"])
    mock_http.post.side_effect = Exception("Connection failed")
    await papers_command(update, context)
    update.message.reply_text.assert_awaited()
    text = update.message.reply_text.call_args[0][0]
    assert "Failed" in text or "failed" in text.lower()


@pytest.mark.asyncio
async def test_papers_empty_results():
    """/papers with no results sends 'No papers found'."""
    update, context, mock_db, _ = _make_update_and_context(args=[])
    mock_db.fetch.return_value = []
    await papers_command(update, context)
    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert "No papers" in text


# ---------------------------------------------------------------------------
# Tests: /stats
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stats_success():
    """/stats shows learning statistics."""
    update, context, _, mock_http = _make_update_and_context()
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {
        "total_cards": 100,
        "due_now": 10,
        "reviewed_today": 5,
        "average_retention": 85.0,
        "streak_days": 7,
    }
    mock_http.get.return_value = mock_resp
    await stats_command(update, context)
    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert "100" in text
    assert "Stats" in text


@pytest.mark.asyncio
async def test_stats_failure():
    """/stats sends error when API call fails."""
    update, context, _, mock_http = _make_update_and_context()
    mock_http.get.side_effect = Exception("timeout")
    await stats_command(update, context)
    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert "Failed" in text or "failed" in text.lower()


# ---------------------------------------------------------------------------
# Tests: /briefing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_briefing_returns_text():
    """/briefing returns composite morning briefing."""
    update, context, mock_db, mock_http = _make_update_and_context()
    # New papers count
    mock_db.fetchrow.return_value = {"cnt": 3}
    # Tasks and milestones
    mock_db.fetch.side_effect = [
        [{"title": "Task 1", "project_name": "Proj"}],  # tasks
        [],  # milestones
    ]
    # Learning engine stats
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"due_now": 5}
    mock_http.get.return_value = mock_resp

    await briefing_command(update, context)
    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert "Briefing" in text or "briefing" in text.lower()


# ---------------------------------------------------------------------------
# Tests: /projects
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_projects_empty():
    """/projects with no active projects sends 'No active projects'."""
    update, context, mock_db, _ = _make_update_and_context()
    mock_db.fetch.return_value = []
    await projects_command(update, context)
    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert "No active" in text


@pytest.mark.asyncio
async def test_projects_with_data():
    """/projects with active projects lists them."""
    update, context, mock_db, _ = _make_update_and_context()
    mock_db.fetch.return_value = [
        {
            "id": 1,
            "name": "Project Alpha",
            "status": "active",
            "description": "A research project",
            "deadline": None,
        },
    ]
    await projects_command(update, context)
    update.message.reply_text.assert_awaited()
    text = update.message.reply_text.call_args_list[0][0][0]
    assert "Project Alpha" in text


# ---------------------------------------------------------------------------
# Tests: /tasks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tasks_no_project_id():
    """/tasks without project_id lists all in-progress tasks."""
    update, context, mock_db, _ = _make_update_and_context(args=[])
    mock_db.fetch.return_value = [
        {"id": 1, "title": "Fix bug", "status": "in_progress", "project_name": "Proj"},
    ]
    await tasks_command(update, context)
    update.message.reply_text.assert_awaited()
    text = update.message.reply_text.call_args[0][0]
    assert "Fix bug" in text


@pytest.mark.asyncio
async def test_tasks_with_project_id():
    """/tasks <project_id> filters tasks by project."""
    update, context, mock_db, _ = _make_update_and_context(args=["1"])
    mock_db.fetch.return_value = [
        {"id": 2, "title": "Write tests", "status": "in_progress", "project_name": "Proj"},
    ]
    await tasks_command(update, context)
    update.message.reply_text.assert_awaited()
    text = update.message.reply_text.call_args[0][0]
    assert "Write tests" in text


@pytest.mark.asyncio
async def test_tasks_empty():
    """/tasks with no tasks sends 'No in-progress tasks'."""
    update, context, mock_db, _ = _make_update_and_context(args=[])
    mock_db.fetch.return_value = []
    await tasks_command(update, context)
    text = update.message.reply_text.call_args[0][0]
    assert "No in-progress" in text


# ---------------------------------------------------------------------------
# Tests: /done
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_done_no_args_prompts():
    """/done without args prompts for usage."""
    update, context, _, _ = _make_update_and_context(args=[])
    await done_command(update, context)
    text = update.message.reply_text.call_args[0][0]
    assert "Usage" in text or "task_id" in text


@pytest.mark.asyncio
async def test_done_nonexistent_error():
    """/done with nonexistent task_id sends error."""
    update, context, mock_db, _ = _make_update_and_context(args=["999"])

    with patch("app.handlers.commands.task_commands.ProjectManager") as mock_pm:
        pm_instance = AsyncMock()
        pm_instance.complete_task.return_value = {}
        mock_pm.return_value = pm_instance

        await done_command(update, context)

    text = update.message.reply_text.call_args[0][0]
    assert "not found" in text.lower() or "999" in text


@pytest.mark.asyncio
async def test_done_success():
    """/done <task_id> marks a task as done."""
    update, context, mock_db, _ = _make_update_and_context(args=["5"])

    with patch("app.handlers.commands.task_commands.ProjectManager") as mock_pm:
        pm_instance = AsyncMock()
        pm_instance.complete_task.return_value = {"id": 5, "status": "done"}
        mock_pm.return_value = pm_instance

        await done_command(update, context)

    text = update.message.reply_text.call_args[0][0]
    assert "done" in text.lower() or "5" in text


# ---------------------------------------------------------------------------
# Tests: /newproject
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_newproject_no_args_prompts():
    """/newproject without args prompts for usage."""
    update, context, _, _ = _make_update_and_context(args=[])
    await newproject_command(update, context)
    text = update.message.reply_text.call_args[0][0]
    assert "Usage" in text or "name" in text


@pytest.mark.asyncio
async def test_newproject_success():
    """/newproject <name> creates a project."""
    update, context, mock_db, _ = _make_update_and_context(args=["My", "Project"])
    mock_db.fetchrow.return_value = {"id": 42}

    await newproject_command(update, context)
    text = update.message.reply_text.call_args[0][0]
    assert "My Project" in text
    assert "42" in text


# ---------------------------------------------------------------------------
# Tests: TG-001 — format_help completeness
# ---------------------------------------------------------------------------


def test_format_help_contains_all_commands():
    """format_help() must include all 13 bot commands."""
    from app.formatters import format_help

    text = format_help()
    expected_commands = [
        "/start",
        "/help",
        "/papers",
        "/briefing",
        "/next",
        "/pulse_now",
        "/review",
        "/stats",
        "/projects",
        "/newproject",
        "/tasks",
        "/done",
        "/focus",
        "/cancel",
    ]
    for cmd in expected_commands:
        assert cmd in text, f"format_help() is missing {cmd!r}"


# ---------------------------------------------------------------------------
# Tests: TG-002 — set_my_commands called in post_init
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_init_calls_set_my_commands():
    """post_init must register at least 12 commands via set_my_commands."""
    # Ensure BotCommand is set in the stub (conftest may already do this).
    sys.modules["telegram"].BotCommand = lambda cmd, desc: (cmd, desc)

    from app.main import post_init  # noqa: PLC0415

    bot_mock = MagicMock()
    bot_mock.set_my_commands = AsyncMock()

    db_pool_mock = AsyncMock()
    db_pool_mock.close = AsyncMock()

    application = MagicMock()
    application.bot = bot_mock
    application.bot_data = {
        "config": _make_config(),
    }

    # Stub create_db_pool and JarvisScheduler so post_init doesn't blow up.
    with (
        patch("app.main.create_db_pool", return_value=db_pool_mock),
        patch("app.main.JarvisScheduler") as mock_sched_cls,
    ):
        sched_instance = AsyncMock()
        sched_instance.load_and_start = AsyncMock()
        mock_sched_cls.return_value = sched_instance

        await post_init(application)

    bot_mock.set_my_commands.assert_awaited_once()
    commands = bot_mock.set_my_commands.call_args[0][0]
    assert len(commands) >= 12, f"Expected ≥12 commands, got {len(commands)}"
