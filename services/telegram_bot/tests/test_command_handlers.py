"""Tests for Telegram bot command handlers.

Covers: /start, /help, /papers, /stats, /briefing, /projects, /tasks, /done, /newproject.
Each handler function is tested directly with mocked Update + Context objects.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jarvis_common.testing import make_bot_config
from jarvis_common.testing_telegram import make_http_response
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


@pytest.fixture(autouse=True)
def _default_auth_patch():
    """Default: auth_required resolves the standard chat as paired user_id=1.

    Multi-user mode requires every authorized chat to have a paired JARVIS
    user_id.  The env-var owner path (telegram_chat_id match) now returns
    (True, None), which the decorator rejects with a pairing message.

    Tests that need (True, None) or a specific user_id should override with
    their own ``patch("telegram_bot.handlers.commands._auth.auth_check", ...)``.
    """
    with patch(
        "telegram_bot.handlers.commands._auth.auth_check",
        new_callable=AsyncMock,
        return_value=(True, 1),
    ):
        yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TEST_CHAT_ID = 12345


def _make_update_and_context(args=None, chat_id=_TEST_CHAT_ID):
    """Build mock Update + Context for command handlers."""
    update = MagicMock()
    update.effective_chat = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_chat.type = "private"
    update.message = MagicMock()
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.args = args or []
    # auth_required stashes the resolved user_id here.  Default to user_id=1
    # so commands that read context.user_data["jarvis_user_id"] get a valid
    # paired user rather than None (which is now blocked by the B4 guard).
    context.user_data = {"jarvis_user_id": 1}

    config = make_bot_config(BotConfig, telegram_chat_id=_TEST_CHAT_ID)
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
    """/papers with no query calls GET /api/papers/feed?view=library."""
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
    text = update.message.reply_text.call_args[0][0]
    assert "Transformers Paper" in text


@pytest.mark.asyncio
async def test_papers_api_failure_sends_error():
    """/papers <query> sends error message when API fails."""
    update, context, _, mock_http = _make_update_and_context(args=["test"])
    mock_http.post.side_effect = Exception("Connection failed")
    await papers_command(update, context)
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
    """/briefing returns composite morning briefing assembled from REST gathers."""
    update, context, _, mock_http = _make_update_and_context()
    # Briefing issues four GETs in order: new-paper feed, stats (due cards),
    # tasks, upcoming milestones.
    mock_http.get.side_effect = [
        make_http_response({"total": 3}),  # fetch_new_paper_count → feed
        make_http_response({"due_now": 5}),  # fetch_due_card_count → stats
        make_http_response([{"title": "Task 1", "project_name": "Proj"}]),  # fetch_tasks
        make_http_response([]),  # fetch_upcoming_milestones
    ]

    await briefing_command(update, context)
    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert "Briefing" in text or "briefing" in text.lower()
    assert "Task 1" in text


@pytest.mark.asyncio
async def test_briefing_partial_degradation_on_milestones_failure():
    """R7: a failed individual gather leaves that section empty, not the whole briefing."""
    update, context, _, mock_http = _make_update_and_context()
    mock_http.get.side_effect = [
        make_http_response({"total": 1}),  # new-paper count OK
        make_http_response({"due_now": 2}),  # due cards OK
        make_http_response([{"title": "Task 1", "project_name": "Proj"}]),  # tasks OK
        make_http_response(None, status=500),  # milestones fail → 5xx
    ]

    await briefing_command(update, context)

    # Briefing still sends, with the working sections rendered.
    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert "Briefing" in text or "briefing" in text.lower()
    assert "Task 1" in text


# ---------------------------------------------------------------------------
# Tests: /projects
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_projects_empty():
    """/projects with no active projects sends 'No active projects'."""
    update, context, _, mock_http = _make_update_and_context()
    mock_http.get.return_value = make_http_response([])
    await projects_command(update, context)
    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert "No active" in text


@pytest.mark.asyncio
async def test_projects_with_data():
    """/projects with active projects lists them via GET /api/projects?status=active."""
    update, context, _, mock_http = _make_update_and_context()
    mock_http.get.return_value = make_http_response(
        [
            {
                "id": 1,
                "name": "Project Alpha",
                "status": "active",
                "description": "A research project",
                "deadline": None,
            },
        ]
    )
    await projects_command(update, context)

    mock_http.get.assert_awaited_once()
    call = mock_http.get.await_args
    assert "/api/projects" in call.args[0]
    assert call.kwargs["params"] == {"status": "active"}
    text = update.message.reply_text.call_args_list[0][0][0]
    assert "Project Alpha" in text


@pytest.mark.asyncio
async def test_projects_backend_failure_sends_graceful_reply():
    """R7: a 5xx from the projects endpoint yields a short failure reply, not a crash."""
    update, context, _, mock_http = _make_update_and_context()
    mock_http.get.return_value = make_http_response(None, status=503)
    await projects_command(update, context)
    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert "Couldn't reach" in text or "try again" in text.lower()


# ---------------------------------------------------------------------------
# Tests: /tasks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tasks_no_project_id():
    """/tasks without project_id lists all in-progress tasks via GET /api/tasks."""
    update, context, _, mock_http = _make_update_and_context(args=[])
    mock_http.get.return_value = make_http_response(
        [{"id": 1, "title": "Fix bug", "status": "in_progress", "project_name": "Proj"}]
    )
    await tasks_command(update, context)

    mock_http.get.assert_awaited_once()
    call = mock_http.get.await_args
    assert "/api/tasks" in call.args[0]
    assert call.kwargs["params"]["status"] == "in_progress"
    assert "project_id" not in call.kwargs["params"]
    text = update.message.reply_text.call_args[0][0]
    assert "Fix bug" in text


@pytest.mark.asyncio
async def test_tasks_with_project_id():
    """/tasks <project_id> filters tasks by project via ?project_id=."""
    update, context, _, mock_http = _make_update_and_context(args=["1"])
    mock_http.get.return_value = make_http_response(
        [{"id": 2, "title": "Write tests", "status": "in_progress", "project_name": "Proj"}]
    )
    await tasks_command(update, context)

    call = mock_http.get.await_args
    assert call.kwargs["params"]["project_id"] == 1
    assert call.kwargs["params"]["status"] == "in_progress"
    text = update.message.reply_text.call_args[0][0]
    assert "Write tests" in text


@pytest.mark.asyncio
async def test_tasks_empty():
    """/tasks with no tasks sends 'No in-progress tasks'."""
    update, context, _, mock_http = _make_update_and_context(args=[])
    mock_http.get.return_value = make_http_response([])
    await tasks_command(update, context)
    text = update.message.reply_text.call_args[0][0]
    assert "No in-progress" in text


@pytest.mark.asyncio
async def test_tasks_backend_failure_sends_graceful_reply():
    """R7: a 5xx from the tasks endpoint yields a short failure reply, not a crash."""
    update, context, _, mock_http = _make_update_and_context(args=[])
    mock_http.get.return_value = make_http_response(None, status=502)
    await tasks_command(update, context)
    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert "Couldn't reach" in text or "try again" in text.lower()


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
    """/done with nonexistent task_id (PUT → 404 → None) sends 'not found'."""
    update, context, _, mock_http = _make_update_and_context(args=["999"])
    mock_http.put.return_value = make_http_response(None, status=404)

    await done_command(update, context)

    mock_http.put.assert_awaited_once()
    call = mock_http.put.await_args
    assert "/api/tasks/999" in call.args[0]
    assert call.kwargs["json"] == {"status": "done"}
    text = update.message.reply_text.call_args[0][0]
    assert "not found" in text.lower() or "999" in text


@pytest.mark.asyncio
async def test_done_success():
    """/done <task_id> marks a task as done via PUT /api/tasks/{id}."""
    update, context, _, mock_http = _make_update_and_context(args=["5"])
    mock_http.put.return_value = make_http_response({"id": 5, "status": "done"})

    await done_command(update, context)

    mock_http.put.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert "done" in text.lower() or "5" in text


@pytest.mark.asyncio
async def test_done_backend_failure_sends_graceful_reply():
    """R7: a 5xx from the task-update endpoint yields a short failure reply, not a crash."""
    update, context, _, mock_http = _make_update_and_context(args=["5"])
    mock_http.put.return_value = make_http_response(None, status=500)

    await done_command(update, context)

    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert "Couldn't reach" in text or "try again" in text.lower()


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
    """/newproject <name> creates a project via POST /api/projects."""
    update, context, _, mock_http = _make_update_and_context(args=["My", "Project"])
    mock_http.post.return_value = make_http_response({"id": 42})

    await newproject_command(update, context)

    mock_http.post.assert_awaited_once()
    call = mock_http.post.await_args
    assert "/api/projects" in call.args[0]
    assert call.kwargs["json"]["name"] == "My Project"
    text = update.message.reply_text.call_args[0][0]
    assert "My Project" in text
    assert "42" in text


# ---------------------------------------------------------------------------
# Tests: TG-001 — format_help completeness
# ---------------------------------------------------------------------------


def test_format_help_contains_all_commands():
    """format_help() must include all bot commands, including pairing/account."""
    from telegram_bot.formatters import format_help

    text = format_help()
    expected_commands = [
        "/start",
        "/help",
        "/papers",
        "/briefing",
        "/next",
        "/inbox",
        "/pulse_now",
        "/review",
        "/stats",
        "/projects",
        "/newproject",
        "/tasks",
        "/done",
        "/focus",
        "/cancel",
        "/pair",
        "/unpair",
        "/whoami",
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
        "config": make_bot_config(BotConfig, telegram_chat_id=_TEST_CHAT_ID),
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
    names = {c.command for c in commands}
    assert {"pair", "unpair", "whoami"}.issubset(names), (
        f"missing pairing commands in autocomplete: {names}"
    )


# ---------------------------------------------------------------------------
# Tests: /focus — clamp guard
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
# Tests: /papers bidi sanitisation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_papers_search_strips_bidi_chars():
    """Inbound search text must have bidi/zero-width chars stripped before forwarding."""
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
    """U+200B (ZERO WIDTH SPACE) must be stripped from inbound search text."""
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
# Per-user scoping of interactive commands
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_briefing_scopes_to_user_via_owner_header_when_paired():
    """/briefing run by a paired user forwards X-Owner-User-Id on every REST gather."""
    update, context, _, mock_http = _make_paired_update_and_context(jarvis_user_id=7)
    context.user_data["jarvis_user_id"] = 7
    mock_http.get.side_effect = [
        make_http_response({"total": 0}),  # new papers
        make_http_response({"due_now": 0}),  # due cards
        make_http_response([]),  # tasks
        make_http_response([]),  # milestones
    ]

    with _paired_auth_patch(7):
        await briefing_command(update, context)

    # All four gathers carry the owner header scoping the response to user 7.
    assert mock_http.get.await_count == 4
    for call in mock_http.get.await_args_list:
        assert call.kwargs["headers"].get("X-Owner-User-Id") == "7"


@pytest.mark.asyncio
async def test_briefing_unpaired_owner_gets_pairing_message():
    """S6: /briefing from an unpaired chat (auth_check -> (False, None)) is
    blocked with the /pair guidance reply."""
    update, context, _, mock_http = _make_update_and_context()
    update.message.reply_text = AsyncMock()

    with patch(
        "telegram_bot.handlers.commands._auth.auth_check",
        new_callable=AsyncMock,
        return_value=(False, None),
    ):
        await briefing_command(update, context)

    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert "pair" in text.lower() or "Settings" in text
    # No backend call for the briefing itself should have been issued
    mock_http.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_tasks_scopes_query_to_paired_user():
    """A12: /tasks run by a paired user forwards X-Owner-User-Id + ?status=in_progress."""
    update, context, _, mock_http = _make_paired_update_and_context(jarvis_user_id=7, args=[])
    context.user_data["jarvis_user_id"] = 7
    mock_http.get.return_value = make_http_response([])

    with _paired_auth_patch(7):
        await tasks_command(update, context)

    call = mock_http.get.await_args
    assert "/api/tasks" in call.args[0]
    assert call.kwargs["headers"].get("X-Owner-User-Id") == "7"
    assert call.kwargs["params"]["status"] == "in_progress"


@pytest.mark.asyncio
async def test_tasks_scopes_query_with_project_filter():
    """/tasks <project_id> from a paired user scopes by owner header + ?project_id=."""
    update, context, _, mock_http = _make_paired_update_and_context(jarvis_user_id=7, args=["3"])
    context.user_data["jarvis_user_id"] = 7
    mock_http.get.return_value = make_http_response([])

    with _paired_auth_patch(7):
        await tasks_command(update, context)

    call = mock_http.get.await_args
    assert call.kwargs["headers"].get("X-Owner-User-Id") == "7"
    assert call.kwargs["params"]["status"] == "in_progress"
    assert call.kwargs["params"]["project_id"] == 3


@pytest.mark.asyncio
async def test_done_passes_user_id_via_owner_header():
    """/done from a paired user forwards X-Owner-User-Id on the PUT /api/tasks call."""
    update, context, _, mock_http = _make_paired_update_and_context(jarvis_user_id=7, args=["5"])
    context.user_data["jarvis_user_id"] = 7
    # 404 → unowned/nonexistent task → "not found"
    mock_http.put.return_value = make_http_response(None, status=404)

    with _paired_auth_patch(7):
        await done_command(update, context)

    mock_http.put.assert_awaited_once()
    call = mock_http.put.await_args
    assert "/api/tasks/5" in call.args[0]
    assert call.kwargs["headers"].get("X-Owner-User-Id") == "7"
    text = update.message.reply_text.call_args[0][0]
    assert "not found" in text.lower() or "5" in text


@pytest.mark.asyncio
async def test_projects_scopes_listing_to_paired_user():
    """A13: /projects from a paired user forwards X-Owner-User-Id + ?status=active."""
    update, context, _, mock_http = _make_paired_update_and_context(jarvis_user_id=7)
    context.user_data["jarvis_user_id"] = 7
    mock_http.get.return_value = make_http_response([])

    with _paired_auth_patch(7):
        await projects_command(update, context)

    call = mock_http.get.await_args
    assert "/api/projects" in call.args[0]
    assert call.kwargs["headers"].get("X-Owner-User-Id") == "7"
    assert call.kwargs["params"] == {"status": "active"}


@pytest.mark.asyncio
async def test_projects_unpaired_owner_gets_pairing_message():
    """S6: /projects from an unpaired chat (auth_check -> (False, None)) is
    blocked with the /pair guidance reply."""
    update, context, _, mock_http = _make_update_and_context()
    update.message.reply_text = AsyncMock()

    with patch(
        "telegram_bot.handlers.commands._auth.auth_check",
        new_callable=AsyncMock,
        return_value=(False, None),
    ):
        await projects_command(update, context)

    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert "pair" in text.lower() or "Settings" in text
    mock_http.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_newproject_passes_user_id_via_owner_header():
    """/newproject from a paired user forwards X-Owner-User-Id on the POST /api/projects call."""
    update, context, _, mock_http = _make_paired_update_and_context(
        jarvis_user_id=7, args=["Alpha"]
    )
    context.user_data["jarvis_user_id"] = 7
    mock_http.post.return_value = make_http_response({"id": 42})

    with _paired_auth_patch(7):
        await newproject_command(update, context)

    mock_http.post.assert_awaited_once()
    call = mock_http.post.await_args
    assert "/api/projects" in call.args[0]
    assert call.kwargs["headers"].get("X-Owner-User-Id") == "7"
    assert call.kwargs["json"]["name"] == "Alpha"


# ---------------------------------------------------------------------------
# Cross-user: X-Owner-User-Id forwarded by paper commands for paired users
# ---------------------------------------------------------------------------


from telegram_bot.handlers.commands.paper_commands import (  # noqa: E402
    inbox_command,
    next_command,
)
from telegram_bot.handlers.commands.system_commands import pulse_now_command  # noqa: E402


@pytest.mark.asyncio
async def test_papers_command_sends_owner_user_id_for_paired_user():
    """/papers sends X-Owner-User-Id when invoked by a paired user."""
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
    """/stats sends X-Owner-User-Id when invoked by a paired user."""
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
    """/next sends X-Owner-User-Id when invoked by a paired user."""
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
    """/inbox sends X-Owner-User-Id when invoked by a paired user."""
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
    """/pulse_now sends X-Owner-User-Id when invoked by a paired user."""
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
    """/briefing sends X-Owner-User-Id + X-API-Key on the /api/stats (due cards) call."""
    update, context, _, mock_http = _make_paired_update_and_context(jarvis_user_id=7)
    context.user_data["jarvis_user_id"] = 7
    mock_http.get.side_effect = [
        make_http_response({"total": 0}),  # new papers
        make_http_response({"due_now": 0}),  # due cards → /api/stats
        make_http_response([]),  # tasks
        make_http_response([]),  # milestones
    ]

    with _paired_auth_patch(7):
        await briefing_command(update, context)

    # Locate the /api/stats (due-card) GET and verify it carries both headers.
    stats_calls = [c for c in mock_http.get.await_args_list if c.args[0].endswith("/api/stats")]
    assert stats_calls, "briefing must call /api/stats for due-card count"
    headers = stats_calls[0].kwargs["headers"]
    assert headers.get("X-Owner-User-Id") == "7"
    assert headers.get("X-API-Key") == "test-key"


# ---------------------------------------------------------------------------
# focus_alarm threads X-Owner-User-Id for paired users
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_focus_alarm_sends_owner_user_id_for_paired_user():
    """focus_alarm callback sends X-Owner-User-Id for the paired user."""
    update, context, _, mock_http = _make_focus_update_and_context(args=["25"])
    # Simulate auth_required having stashed the jarvis_user_id
    context.user_data["jarvis_user_id"] = 42

    captured_callbacks: list = []

    def _capture_run_once(callback, delay, **kwargs):
        captured_callbacks.append((callback, kwargs.get("data")))

    context.job_queue.run_once.side_effect = _capture_run_once

    with _paired_auth_patch(42):
        await focus_command(update, context)

    assert captured_callbacks, "run_once was not called"
    callback, job_data = captured_callbacks[0]
    assert isinstance(job_data, tuple), "job data must be a (minutes, user_id) tuple"
    _, user_id_in_data = job_data
    assert user_id_in_data == 42, f"expected user_id=42 in job data, got {user_id_in_data}"

    # Simulate the alarm firing
    mock_http.post.return_value = MagicMock(raise_for_status=MagicMock())

    alarm_context = MagicMock()
    alarm_context.job = MagicMock()
    alarm_context.job.chat_id = 99999
    alarm_context.job.data = job_data
    alarm_context.bot.send_message = AsyncMock()
    alarm_context.application = context.application

    await callback(alarm_context)

    mock_http.post.assert_awaited_once()
    headers = mock_http.post.await_args[1]["headers"]
    assert headers.get("X-Owner-User-Id") == "42", (
        f"focus_alarm must send X-Owner-User-Id=42, headers={headers}"
    )
    assert headers.get("X-API-Key") == "test-key"


# ---------------------------------------------------------------------------
# B4: authorized-but-unpaired caller gets pairing instruction, no backend call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_required_blocks_unpaired_owner_with_pairing_message():
    """S6: an unpaired chat (auth_check -> (False, None)) gets a /pair guidance reply."""
    update, context, _, mock_http = _make_update_and_context()
    update.message.reply_text = AsyncMock()

    # Pairing is the sole identity mechanism: an unpaired chat is (False, None).
    with patch(
        "telegram_bot.handlers.commands._auth.auth_check",
        new_callable=AsyncMock,
        return_value=(False, None),
    ):
        await help_command(update, context)

    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert "pair" in text.lower() or "Settings" in text, (
        f"Expected pairing instruction in reply, got: {text!r}"
    )
    # No backend HTTP call must have been made
    mock_http.get.assert_not_awaited()
    mock_http.post.assert_not_awaited()
