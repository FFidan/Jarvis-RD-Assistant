"""Tests for Telegram bot command handlers.

Covers: /start, /help, /papers, /stats, /briefing, /projects, /tasks, /done, /newproject.
Each handler function is tested directly with mocked Update + Context objects.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import SecretStr
from telegram_bot.config import BotConfig
from telegram_bot.handlers import rate_limit as _rate_limit_mod
from telegram_bot.handlers.commands import (  # noqa: E402
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
from telegram_bot.handlers.commands.paper_commands import _inbox_keyboard
from telegram_bot.handlers.commands.system_commands import focus_command

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_rate_limit_state():  # pyright: ignore[reportUnusedFunction]
    """Clear the rate-limiter's in-memory timestamp store before every test.

    Command handlers decorated with @rate_limit share a module-level
    defaultdict keyed by ``chat_id:func_name``.  Without this fixture, tests
    sharing the same chat_id accumulate timestamps and can trip the limit mid
    suite.
    """
    _rate_limit_mod._timestamps.clear()
    yield
    _rate_limit_mod._timestamps.clear()


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
        jarvis_api_key=SecretStr("test-key"),
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
    # auth_required decorator stashes the resolved user_id here; default to an
    # empty dict so tests not exercising per-user scoping behave as the env-var
    # owner path (jarvis_user_id absent => None).
    context.user_data = {}

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


def _make_paired_update_and_context(jarvis_user_id, args=None):
    """Build an Update/Context for a paired multi-tenant chat.

    The chat_id does NOT match the env-var config, so the auth_required
    decorator would normally consult the pairing table.  Callers patch
    ``telegram_bot.handlers.commands._auth.auth_check`` to short-circuit DB
    lookups (the per-command tests in this module exercise the SQL the
    decorator's stashed ``jarvis_user_id`` enables, not auth itself).
    """
    update, context, mock_db, mock_http = _make_update_and_context(args=args, chat_id=99999)
    context.user_data = {}
    return update, context, mock_db, mock_http


def _paired_auth_patch(jarvis_user_id):
    """Patch the decorator's auth_check to grant access as the given user."""
    return patch(
        "telegram_bot.handlers.commands._auth.auth_check",
        new_callable=AsyncMock,
        return_value=(True, jarvis_user_id),
    )


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


def test_inbox_keyboard_uses_feed_thumbs_for_system_discovered_papers():
    """Telegram /inbox feedback buttons should write feed_thumbs, not pulse_thumbs."""
    markup = _inbox_keyboard(42, discovery_origin="recommender")
    callback_data = [button.callback_data for row in markup.inline_keyboard for button in row]

    assert "paper:feedback_pos:42:feed_thumbs" in callback_data
    assert "paper:feedback_neg:42:feed_thumbs" in callback_data
    assert all("pulse_thumbs" not in value for value in callback_data if value)


def test_inbox_keyboard_hides_feedback_for_user_initiated_papers():
    """User-initiated inbox papers do not render recommendation-feedback buttons."""
    markup = _inbox_keyboard(42, discovery_origin="user_initiated")
    callback_data = [button.callback_data for row in markup.inline_keyboard for button in row]

    assert "paper:save:42" in callback_data
    assert "paper:trash:42" in callback_data
    assert not any("feedback_" in value for value in callback_data if value)


@pytest.mark.asyncio
async def test_papers_no_args_lists_library_via_api():
    """/papers with no query calls GET /api/papers/feed?view=library (Wave 3)."""
    update, context, _, mock_http = _make_update_and_context(args=[])
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {
        "papers": [
            {
                "id": 1,
                "title": "Paper A",
                "authors": ["Author"],
                "published_date": None,
                "source_type": "arxiv",
                "url": "http://example.com",
                "summary_brief": None,
                "tldr": None,
                "discovery_origin": "user_initiated",
            },
        ],
        "total": 1,
    }
    mock_http.get.return_value = mock_resp
    await papers_command(update, context)
    mock_http.get.assert_awaited_once()
    call = mock_http.get.await_args
    assert "/api/papers/feed" in call.args[0]
    assert call.kwargs.get("params", {}).get("view") == "library"
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
async def test_papers_empty_library_shows_empty_state():
    """/papers shows the empty-Library message when feed?view=library returns []."""
    update, context, _, mock_http = _make_update_and_context(args=[])
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"papers": [], "total": 0}
    mock_http.get.return_value = mock_resp
    await papers_command(update, context)
    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert "Library is empty" in text


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
    update, context, _mock_db, _ = _make_update_and_context(args=["999"])

    with patch("telegram_bot.handlers.commands.task_commands.ProjectManager") as mock_pm:
        pm_instance = AsyncMock()
        pm_instance.complete_task.return_value = {}
        mock_pm.return_value = pm_instance

        await done_command(update, context)

    text = update.message.reply_text.call_args[0][0]
    assert "not found" in text.lower() or "999" in text


@pytest.mark.asyncio
async def test_done_success():
    """/done <task_id> marks a task as done."""
    update, context, _mock_db, _ = _make_update_and_context(args=["5"])

    with patch("telegram_bot.handlers.commands.task_commands.ProjectManager") as mock_pm:
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
    from telegram_bot.formatters import format_help

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
    # BotCommand is now the real class from the installed telegram package.

    from telegram_bot.main import post_init  # noqa: PLC0415

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
        patch("telegram_bot.main.create_db_pool", return_value=db_pool_mock),
        patch("telegram_bot.main.JarvisScheduler") as mock_sched_cls,
    ):
        sched_instance = AsyncMock()
        sched_instance.load_and_start = AsyncMock()
        mock_sched_cls.return_value = sched_instance

        await post_init(application)

    bot_mock.set_my_commands.assert_awaited_once()
    commands = bot_mock.set_my_commands.call_args[0][0]
    assert len(commands) >= 12, f"Expected ≥12 commands, got {len(commands)}"


# ---------------------------------------------------------------------------
# Tests: /focus — W4-4 clamp guard
# ---------------------------------------------------------------------------


def _make_focus_update_and_context(args=None, chat_id=_TEST_CHAT_ID):
    """Build a mock Update + Context for focus_command (includes job_queue)."""
    update, context, mock_db, mock_http = _make_update_and_context(args=args, chat_id=chat_id)
    context.job_queue = MagicMock()
    context.job_queue.get_jobs_by_name = MagicMock(return_value=[])
    context.job_queue.run_once = MagicMock()
    return update, context, mock_db, mock_http


@pytest.mark.asyncio
async def test_focus_negative_minutes_rejected_with_help():
    """/focus -1 is rejected with help text; timer must NOT be started."""
    update, context, _, _ = _make_focus_update_and_context(args=["-1"])

    await focus_command(update, context)

    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert "minute" in text.lower() or "focus" in text.lower()
    context.job_queue.run_once.assert_not_called()


@pytest.mark.asyncio
async def test_focus_zero_minutes_rejected_with_help():
    """/focus 0 is rejected with help text; timer must NOT be started."""
    update, context, _, _ = _make_focus_update_and_context(args=["0"])

    await focus_command(update, context)

    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert "minute" in text.lower() or "focus" in text.lower()
    context.job_queue.run_once.assert_not_called()


@pytest.mark.asyncio
async def test_focus_positive_minutes_accepted():
    """/focus 25 proceeds normally — timer is scheduled."""
    update, context, _, _ = _make_focus_update_and_context(args=["25"])

    await focus_command(update, context)

    context.job_queue.run_once.assert_called_once()
    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert "25" in text


# ---------------------------------------------------------------------------
# Tests: /papers bidi sanitisation — W4-4
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_papers_search_strips_bidi_chars():
    """W4-4: inbound search text must have bidi/zero-width chars stripped before forwarding."""
    # U+202E (RTL OVERRIDE) embedded in the query string
    bidi_query = "neural‮nets"
    clean_query = "neuralnets"

    update, context, _, mock_http = _make_update_and_context(args=[bidi_query])
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = []
    mock_http.post.return_value = mock_resp

    await papers_command(update, context)

    mock_http.post.assert_awaited_once()
    call_kwargs = mock_http.post.await_args[1]
    sent_query = call_kwargs["json"]["query"]
    assert "‮" not in sent_query, "RTL override must be stripped before forwarding"
    assert clean_query == sent_query, f"Expected {clean_query!r}, got {sent_query!r}"


@pytest.mark.asyncio
async def test_papers_search_strips_zero_width_space():
    """W4-4: U+200B (ZERO WIDTH SPACE) must be stripped from inbound search text."""
    query_with_zwsp = "machine​learning"
    clean_query = "machinelearning"

    update, context, _, mock_http = _make_update_and_context(args=[query_with_zwsp])
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = []
    mock_http.post.return_value = mock_resp

    await papers_command(update, context)

    mock_http.post.assert_awaited_once()
    call_kwargs = mock_http.post.await_args[1]
    sent_query = call_kwargs["json"]["query"]
    assert "​" not in sent_query, "Zero-width space must be stripped before forwarding"
    assert sent_query == clean_query


# ---------------------------------------------------------------------------
# Wave-0 C1/C2: per-user scoping of interactive commands
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_briefing_scopes_papers_to_user_library_when_paired():
    """/briefing run by a paired user counts papers via user_library JOIN."""
    update, context, mock_db, mock_http = _make_paired_update_and_context(jarvis_user_id=7)
    mock_db.fetchrow.return_value = {"cnt": 0}
    mock_db.fetch.side_effect = [[], []]
    stats_resp = MagicMock()
    stats_resp.raise_for_status = MagicMock()
    stats_resp.json.return_value = {"due_now": 0}
    mock_http.get.return_value = stats_resp

    with _paired_auth_patch(7):
        await briefing_command(update, context)

    papers_sql = mock_db.fetchrow.await_args.args[0]
    assert "user_library" in papers_sql
    assert "ul.user_id = $1" in papers_sql
    assert mock_db.fetchrow.await_args.args[1] == 7

    fetch_calls = mock_db.fetch.await_args_list
    tasks_sql = fetch_calls[0].args[0]
    milestones_sql = fetch_calls[1].args[0]
    assert "t.user_id IS NOT DISTINCT FROM" in tasks_sql
    assert "m.user_id IS NOT DISTINCT FROM" in milestones_sql


@pytest.mark.asyncio
async def test_briefing_owner_path_remains_unscoped():
    """/briefing run by the env-var owner (jarvis_user_id=None) keeps legacy SQL."""
    update, context, mock_db, mock_http = _make_update_and_context()
    mock_db.fetchrow.return_value = {"cnt": 0}
    mock_db.fetch.side_effect = [[], []]
    stats_resp = MagicMock()
    stats_resp.raise_for_status = MagicMock()
    stats_resp.json.return_value = {"due_now": 0}
    mock_http.get.return_value = stats_resp

    await briefing_command(update, context)

    papers_sql = mock_db.fetchrow.await_args.args[0]
    assert "user_library" not in papers_sql
    fetch_calls = mock_db.fetch.await_args_list
    assert "user_id" not in fetch_calls[0].args[0]
    assert "user_id" not in fetch_calls[1].args[0]


@pytest.mark.asyncio
async def test_tasks_scopes_query_to_paired_user():
    """/tasks run by a paired user adds an `t.user_id IS NOT DISTINCT FROM` filter."""
    update, context, mock_db, _ = _make_paired_update_and_context(jarvis_user_id=7, args=[])
    mock_db.fetch.return_value = []

    with _paired_auth_patch(7):
        await tasks_command(update, context)

    sql, *params = mock_db.fetch.await_args.args
    assert "t.user_id IS NOT DISTINCT FROM $1" in sql
    assert params == [7]


@pytest.mark.asyncio
async def test_tasks_scopes_query_with_project_filter():
    """/tasks <project_id> from a paired user scopes by BOTH user_id and project_id."""
    update, context, mock_db, _ = _make_paired_update_and_context(jarvis_user_id=7, args=["3"])
    mock_db.fetch.return_value = []

    with _paired_auth_patch(7):
        await tasks_command(update, context)

    sql, *params = mock_db.fetch.await_args.args
    assert "t.user_id IS NOT DISTINCT FROM $1" in sql
    assert "t.project_id = $2" in sql
    assert params == [7, 3]


@pytest.mark.asyncio
async def test_done_passes_user_id_to_complete_task():
    """/done from a paired user forwards jarvis_user_id to ProjectManager."""
    update, context, _mock_db, _ = _make_paired_update_and_context(jarvis_user_id=7, args=["5"])

    with (
        _paired_auth_patch(7),
        patch("telegram_bot.handlers.commands.task_commands.ProjectManager") as mock_pm,
    ):
        pm_instance = AsyncMock()
        pm_instance.complete_task.return_value = {}
        mock_pm.return_value = pm_instance

        await done_command(update, context)

    # Task wasn't owned -> reply says "not found"
    text = update.message.reply_text.call_args[0][0]
    assert "not found" in text.lower() or "5" in text
    pm_instance.complete_task.assert_awaited_once_with(5, user_id=7)


@pytest.mark.asyncio
async def test_projects_scopes_listing_to_paired_user():
    """/projects from a paired user filters by `user_id IS NOT DISTINCT FROM`."""
    update, context, mock_db, _ = _make_paired_update_and_context(jarvis_user_id=7)
    mock_db.fetch.return_value = []

    with _paired_auth_patch(7):
        await projects_command(update, context)

    sql, *params = mock_db.fetch.await_args.args
    assert "user_id IS NOT DISTINCT FROM $1" in sql
    assert params == [7]


@pytest.mark.asyncio
async def test_projects_owner_path_remains_unscoped():
    """/projects from the env-var owner keeps legacy unscoped SQL."""
    update, context, mock_db, _ = _make_update_and_context()
    mock_db.fetch.return_value = []

    await projects_command(update, context)

    sql = mock_db.fetch.await_args.args[0]
    assert "user_id" not in sql


@pytest.mark.asyncio
async def test_newproject_passes_user_id_to_create_project():
    """/newproject from a paired user forwards jarvis_user_id to ProjectManager."""
    update, context, _mock_db, _ = _make_paired_update_and_context(jarvis_user_id=7, args=["Alpha"])

    with (
        _paired_auth_patch(7),
        patch("telegram_bot.handlers.commands.project_commands.ProjectManager") as mock_pm,
    ):
        pm_instance = AsyncMock()
        pm_instance.create_project.return_value = {"id": 42}
        mock_pm.return_value = pm_instance

        await newproject_command(update, context)

    pm_instance.create_project.assert_awaited_once_with("Alpha", user_id=7)


# ---------------------------------------------------------------------------
# WS-CROSS-USER: X-Owner-User-Id forwarded by paper commands for paired users
# ---------------------------------------------------------------------------


from telegram_bot.handlers.commands.paper_commands import (  # noqa: E402
    inbox_command,
    next_command,
)
from telegram_bot.handlers.commands.system_commands import pulse_now_command  # noqa: E402


@pytest.mark.asyncio
async def test_papers_command_sends_owner_user_id_for_paired_user():
    """WS-CROSS-USER: /papers sends X-Owner-User-Id when invoked by a paired user."""
    update, context, _, mock_http = _make_paired_update_and_context(jarvis_user_id=7, args=[])
    context.user_data["jarvis_user_id"] = 7
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"papers": [], "total": 0}
    mock_http.get.return_value = mock_resp

    with _paired_auth_patch(7):
        await papers_command(update, context)

    mock_http.get.assert_awaited_once()
    headers = mock_http.get.await_args[1]["headers"]
    assert headers.get("X-Owner-User-Id") == "7"
    assert headers.get("X-API-Key") == "test-key"


@pytest.mark.asyncio
async def test_stats_command_sends_owner_user_id_for_paired_user():
    """WS-CROSS-USER: /stats sends X-Owner-User-Id when invoked by a paired user."""
    update, context, _, mock_http = _make_paired_update_and_context(jarvis_user_id=7)
    context.user_data["jarvis_user_id"] = 7
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {
        "total_cards": 0,
        "due_now": 0,
        "reviewed_today": 0,
        "average_retention": 0.0,
        "streak_days": 0,
    }
    mock_http.get.return_value = mock_resp

    with _paired_auth_patch(7):
        await stats_command(update, context)

    mock_http.get.assert_awaited_once()
    headers = mock_http.get.await_args[1]["headers"]
    assert headers.get("X-Owner-User-Id") == "7"
    assert headers.get("X-API-Key") == "test-key"


@pytest.mark.asyncio
async def test_next_command_sends_owner_user_id_for_paired_user():
    """WS-CROSS-USER: /next sends X-Owner-User-Id when invoked by a paired user."""
    update, context, _, mock_http = _make_paired_update_and_context(jarvis_user_id=7)
    context.user_data["jarvis_user_id"] = 7
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"cards": []}
    mock_http.get.return_value = mock_resp

    with _paired_auth_patch(7):
        await next_command(update, context)

    mock_http.get.assert_awaited_once()
    headers = mock_http.get.await_args[1]["headers"]
    assert headers.get("X-Owner-User-Id") == "7"


@pytest.mark.asyncio
async def test_inbox_command_sends_owner_user_id_for_paired_user():
    """WS-CROSS-USER: /inbox sends X-Owner-User-Id when invoked by a paired user."""
    update, context, _, mock_http = _make_paired_update_and_context(jarvis_user_id=7)
    context.user_data["jarvis_user_id"] = 7
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"papers": []}
    mock_http.get.return_value = mock_resp

    with _paired_auth_patch(7):
        await inbox_command(update, context)

    mock_http.get.assert_awaited_once()
    headers = mock_http.get.await_args[1]["headers"]
    assert headers.get("X-Owner-User-Id") == "7"


@pytest.mark.asyncio
async def test_pulse_now_command_sends_owner_user_id_for_paired_user():
    """WS-CROSS-USER: /pulse_now sends X-Owner-User-Id when invoked by a paired user."""
    update, context, _, mock_http = _make_paired_update_and_context(jarvis_user_id=7)
    context.user_data["jarvis_user_id"] = 7
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_http.post.return_value = mock_resp

    with _paired_auth_patch(7):
        await pulse_now_command(update, context)

    mock_http.post.assert_awaited_once()
    headers = mock_http.post.await_args[1]["headers"]
    assert headers.get("X-Owner-User-Id") == "7"


@pytest.mark.asyncio
async def test_briefing_command_sends_owner_user_id_to_stats_endpoint():
    """WS-CROSS-USER: /briefing sends X-Owner-User-Id on the /api/stats HTTP call."""
    update, context, mock_db, mock_http = _make_paired_update_and_context(jarvis_user_id=7)
    context.user_data["jarvis_user_id"] = 7
    mock_db.fetchrow.return_value = {"cnt": 0}
    mock_db.fetch.side_effect = [[], []]
    stats_resp = MagicMock()
    stats_resp.raise_for_status = MagicMock()
    stats_resp.json.return_value = {"due_now": 0}
    mock_http.get.return_value = stats_resp

    with _paired_auth_patch(7):
        await briefing_command(update, context)

    mock_http.get.assert_awaited_once()
    headers = mock_http.get.await_args[1]["headers"]
    assert headers.get("X-Owner-User-Id") == "7"
    assert headers.get("X-API-Key") == "test-key"
