"""Tests for Telegram bot command handlers.

Covers: /start, /help, /papers, /stats, /briefing, /projects, /tasks, /done, /newproject.
Each handler function is tested directly with mocked Update + Context objects.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jarvis_common.testing import PTBContextOptions, make_bot_config, make_ptb_context
from jarvis_common.testing_telegram import make_http_response
from telegram_bot.config import BotConfig
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
from telegram_bot.handlers.commands.paper_commands import _inbox_keyboard, discover_command
from telegram_bot.handlers.commands.system_commands import focus_command

pytestmark = pytest.mark.usefixtures("_clear_rate_limit_state")

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _default_auth_patch():
    """Default: auth_required resolves the standard chat as paired user_id=1.

    Multi-user mode requires every authorized chat to have a paired JARVIS
    user_id. Unpaired chats return
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

    config = make_bot_config(BotConfig)
    mock_db = AsyncMock()
    mock_http = AsyncMock()
    # auth_required stashes the resolved user_id in user_data.  Default to
    # user_id=1 so commands that read context.user_data["jarvis_user_id"] get a
    # valid paired user rather than None (which is now blocked by the B4 guard).
    context = make_ptb_context(
        mock_db,
        config,
        options=PTBContextOptions(
            http_client=mock_http, args=args or [], user_data={"jarvis_user_id": 1}
        ),
    )

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
    """/papers <query> searches the caller's library through the feed endpoint."""
    update, context, _, mock_http = _make_update_and_context(args=["transformer"])
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status = MagicMock()
    # The real GET /api/papers/feed envelope (FeedResponse), not a bare list.
    mock_resp.json.return_value = {
        "papers": [
            {
                "id": 1,
                "title": "Transformers Paper",
                "authors": ["Author"],
                "published_date": None,
                "source_type": "arxiv",
                "url": "http://example.com",
                "tldr": None,
                "summary_brief": None,
                "state": "to_read",
                "starred": False,
                "discovery_origin": "user_initiated",
            },
        ],
        "total": 1,
        "search_mode": "bm25",
    }
    mock_http.get.return_value = mock_resp

    await papers_command(update, context)

    mock_http.post.assert_not_awaited()
    mock_http.get.assert_awaited_once()
    call = mock_http.get.await_args
    assert "/api/papers/feed" in call.args[0]
    params = call.kwargs["params"]
    assert params["view"] == "library"
    assert params["q"] == "transformer"
    text = update.message.reply_text.call_args[0][0]
    assert "Transformers Paper" in text


@pytest.mark.asyncio
async def test_papers_with_query_and_no_match_points_at_discover():
    """/papers <query> with no library hit must not claim the library is empty."""
    update, context, _, mock_http = _make_update_and_context(args=["quantum"])
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"papers": [], "total": 0, "search_mode": "bm25"}
    mock_http.get.return_value = mock_resp

    await papers_command(update, context)

    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert 'No library papers match "quantum"' in text
    assert "/discover quantum" in text
    assert "Library is empty" not in text


@pytest.mark.asyncio
async def test_papers_api_failure_sends_error():
    """/papers <query> sends error message when API fails."""
    update, context, _, mock_http = _make_update_and_context(args=["test"])
    mock_http.get.side_effect = Exception("Connection failed")
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


@pytest.mark.asyncio
async def test_papers_unrecognised_feed_payload_reports_a_failure():
    """A feed payload that is not the documented envelope must not read as empty."""
    update, context, _, mock_http = _make_update_and_context(args=[])
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = [{"id": 1, "title": "Paper A"}]
    mock_http.get.return_value = mock_resp

    await papers_command(update, context)

    text = update.message.reply_text.call_args[0][0]
    assert "Failed to load library" in text
    assert "Library is empty" not in text


# ---------------------------------------------------------------------------
# Tests: /discover
# ---------------------------------------------------------------------------


def _multi_source_search_payload(*, failed: list[dict] | None = None) -> dict:
    """Return a MultiSourceSearchResponse payload as POST /api/search returns it.

    ``_persist_search_results`` saves each result independently and guarantees
    ``len(saved) + len(failed) == len(results)``, so every requested failure
    removes one row from ``saved`` here too.
    """
    failures = failed or []
    all_saved = [{"id": 11, "title": "Paper A"}, {"id": 12, "title": "Paper B"}]
    return {
        "results": [
            {"title": "Paper A", "source_type": "arxiv", "external_id": "a1"},
            {"title": "Paper B", "source_type": "pubmed", "external_id": "b1"},
        ],
        "total": 2,
        "per_source_counts": {
            "arxiv": 1,
            "semantic_scholar": 0,
            "openalex": 0,
            "pubmed": 1,
        },
        "degraded_sources": ["openalex"],
        "saved": all_saved[: len(all_saved) - len(failures)],
        "failed": failures,
    }


@pytest.mark.asyncio
async def test_discover_searches_all_sources_and_states_the_library_write():
    """/discover <query> searches the four standard sources and says results were saved."""
    update, context, _, mock_http = _make_update_and_context(args=["transformer"])
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = _multi_source_search_payload()
    mock_http.post.return_value = mock_resp

    await discover_command(update, context)

    mock_http.post.assert_awaited_once()
    call = mock_http.post.await_args
    assert call.args[0].endswith("/api/search")
    body = call.kwargs["json"]
    assert body["query"] == "transformer"
    assert set(body["source_types"]) == {"arxiv", "semantic_scholar", "openalex", "pubmed"}
    # The backend worst case observed for this call is 70.5 s.
    assert call.kwargs["timeout"] > 70.5

    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert "Found 2 papers and saved 2 to your library." in text
    assert "arxiv: 1" in text
    assert "pubmed: 1" in text
    assert "openalex" in text


@pytest.mark.asyncio
async def test_discover_reports_papers_that_could_not_be_saved():
    """/discover must not claim every result was saved when persistence failed."""
    update, context, _, mock_http = _make_update_and_context(args=["transformer"])
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = _multi_source_search_payload(
        failed=[{"external_id": "b1", "error": "conflict"}]
    )
    mock_http.post.return_value = mock_resp

    await discover_command(update, context)

    text = update.message.reply_text.call_args[0][0]
    # The headline itself must carry the saved count the backend reported (1 of
    # 2), not the number of results found.
    assert "Found 2 papers and saved 1 to your library." in text
    assert "1 paper could not be saved." in text


@pytest.mark.asyncio
async def test_discover_does_not_claim_a_library_write_when_nothing_was_saved():
    """/discover must not report a saved count when the backend persisted nothing."""
    update, context, _, mock_http = _make_update_and_context(args=["transformer"])
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = _multi_source_search_payload(
        failed=[
            {"external_id": "a1", "error": "conflict"},
            {"external_id": "b1", "error": "conflict"},
        ]
    )
    mock_http.post.return_value = mock_resp

    await discover_command(update, context)

    text = update.message.reply_text.call_args[0][0]
    assert "Found 2 papers, but none could be saved to your library." in text
    assert "saved them to your library" not in text


@pytest.mark.asyncio
async def test_discover_without_a_query_shows_usage():
    """/discover needs a query; it must not fire a source-wide search without one."""
    update, context, _, mock_http = _make_update_and_context(args=[])

    await discover_command(update, context)

    mock_http.post.assert_not_awaited()
    text = update.message.reply_text.call_args[0][0]
    assert "/discover" in text


@pytest.mark.asyncio
async def test_discover_with_no_results_suggests_a_different_query():
    """/discover reports an empty external search without claiming a library write."""
    update, context, _, mock_http = _make_update_and_context(args=["obscure"])
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {
        "results": [],
        "total": 0,
        "per_source_counts": {},
        "degraded_sources": [],
        "saved": [],
        "failed": [],
    }
    mock_http.post.return_value = mock_resp

    await discover_command(update, context)

    text = update.message.reply_text.call_args[0][0]
    assert "saved" not in text.lower()
    assert "obscure" in text


@pytest.mark.asyncio
async def test_discover_api_failure_sends_error():
    """/discover surfaces a backend failure instead of a silent empty result."""
    update, context, _, mock_http = _make_update_and_context(args=["transformer"])
    mock_http.post.side_effect = Exception("Connection failed")

    await discover_command(update, context)

    text = update.message.reply_text.call_args[0][0]
    assert "failed" in text.lower()


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
        "/discover",
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
    """post_init must register the exact catalog-backed autocomplete menu."""
    # BotCommand is now the real class from the installed telegram package.

    from telegram_bot.command_catalog import menu_command_specs  # noqa: PLC0415
    from telegram_bot.main import post_init  # noqa: PLC0415

    bot_mock = MagicMock()
    bot_mock.set_my_commands = AsyncMock()

    platform_client = AsyncMock()
    backend_client = AsyncMock()

    application = MagicMock()
    application.bot = bot_mock
    application.bot_data = {
        "config": make_bot_config(BotConfig),
    }

    with (
        patch(
            "telegram_bot.main.pinned_async_client",
            side_effect=[platform_client, backend_client],
        ),
        patch("telegram_bot.main.JarvisScheduler") as mock_sched_cls,
        patch("telegram_bot.main.start_internal_server", new_callable=AsyncMock),
        patch("telegram_bot.main._secrets_rotation_watcher", new_callable=AsyncMock),
    ):
        sched_instance = AsyncMock()
        sched_instance.load_and_start = AsyncMock()
        mock_sched_cls.return_value = sched_instance

        await post_init(application)
        await asyncio.sleep(0)

    bot_mock.set_my_commands.assert_awaited_once()
    commands = bot_mock.set_my_commands.call_args[0][0]
    names = {c.command for c in commands}
    assert names == {spec.name for spec in menu_command_specs()}
    assert application.bot_data["platform_client"] is platform_client
    assert application.bot_data["http_client"] is backend_client


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
    """/focus 25 starts the durable server timer without a local PTB job."""
    update, context, _, _ = _make_focus_update_and_context(args=["25"])

    with patch(
        "telegram_bot.handlers.commands.system_commands.services_client.start_focus_session",
        new_callable=AsyncMock,
    ) as start_focus:
        await focus_command(update, context)

    start_focus.assert_awaited_once()
    assert start_focus.await_args.args[2:] == (1, 1500)
    context.job_queue.run_once.assert_not_called()
    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert "25" in text
    assert "paused" in text.lower()


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
    mock_resp.json.return_value = {"papers": [], "total": 0, "search_mode": "bm25"}
    mock_http.get.return_value = mock_resp

    await papers_command(update, context)

    mock_http.get.assert_awaited_once()
    sent_query = mock_http.get.await_args.kwargs["params"]["q"]
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
    mock_resp.json.return_value = {"papers": [], "total": 0, "search_mode": "bm25"}
    mock_http.get.return_value = mock_resp

    await papers_command(update, context)

    mock_http.get.assert_awaited_once()
    sent_query = mock_http.get.await_args.kwargs["params"]["q"]
    assert "​" not in sent_query, "Zero-width space must be stripped before forwarding"
    assert sent_query == clean_query


@pytest.mark.asyncio
async def test_discover_strips_bidi_chars():
    """/discover forwards its query to external sources, so it must sanitize it too."""
    update, context, _, mock_http = _make_update_and_context(args=["neural‮nets"])
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = _multi_source_search_payload()
    mock_http.post.return_value = mock_resp

    await discover_command(update, context)

    sent_query = mock_http.post.await_args.kwargs["json"]["query"]
    assert sent_query == "neuralnets"


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
    assert "X-API-Key" not in headers


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
    assert "X-API-Key" not in headers


@pytest.mark.asyncio
async def test_next_command_sends_owner_user_id_for_paired_user():
    """/next sends X-Owner-User-Id when invoked by a paired user."""
    update, context, _, mock_http = _make_paired_update_and_context(jarvis_user_id=7)
    context.user_data["jarvis_user_id"] = 7
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = None
    mock_http.get.return_value = mock_resp

    with _paired_auth_patch(7):
        await next_command(update, context)

    mock_http.get.assert_awaited_once()
    headers = mock_http.get.await_args[1]["headers"]
    assert headers.get("X-Owner-User-Id") == "7"


@pytest.mark.asyncio
async def test_next_command_labels_stale_unverified_pulse_without_diagnostic_leak():
    update, context, _, mock_http = _make_paired_update_and_context(jarvis_user_id=7)
    context.user_data["jarvis_user_id"] = 7
    mock_http.get.return_value = make_http_response(
        {
            "deck_id": 7,
            "deck_date": "2026-08-07",
            "card_count": 1,
            "generated_at": "2026-08-07T06:00:00+00:00",
            "cards": [
                {
                    "card_id": 11,
                    "paper_id": 12,
                    "paper_title": "A paper",
                    "paper_authors": ["A researcher"],
                    "paper_url": "https://example.org/paper",
                    "rank": 1,
                    "score": 0.8,
                    "llm_relevance": 8,
                    "llm_novelty": 7,
                    "reasoning": "Relevant to the configured topic.",
                    "signals": {"recency": 0.5},
                    "reasoning_verified": False,
                    "reasoning_confidence": "UNVERIFIED",
                }
            ],
            "stats": {},
            "degraded_reason": "sensitive backend diagnostic",
            "is_stale": True,
            "stale_age_days": 2,
        }
    )

    with _paired_auth_patch(7):
        await next_command(update, context)

    text = update.message.reply_text.await_args.args[0]
    assert "Earlier Pulse from August 07 (2 days old)" in text
    assert "reduced signals" in text
    assert "Unverified" in text
    assert "sensitive backend diagnostic" not in text


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
    """/briefing sends only the paired-user marker to the stats client."""
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

    # Locate the /api/stats GET and verify the local assertion marker.
    stats_calls = [c for c in mock_http.get.await_args_list if c.args[0].endswith("/api/stats")]
    assert stats_calls, "briefing must call /api/stats for due-card count"
    headers = stats_calls[0].kwargs["headers"]
    assert headers.get("X-Owner-User-Id") == "7"
    assert "X-API-Key" not in headers


# ---------------------------------------------------------------------------
# focus_alarm threads X-Owner-User-Id for paired users
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_focus_start_sends_owner_user_id_for_paired_user():
    """The durable focus start request is scoped to the paired user."""
    update, context, _, mock_http = _make_focus_update_and_context(args=["25"])
    context.user_data["jarvis_user_id"] = 42
    mock_http.post.return_value = make_http_response(
        {
            "id": 9,
            "state": "active",
            "source": "telegram",
            "duration_seconds": 1500,
            "remaining_seconds": 1500,
            "started_at": "2026-08-09T12:00:00+00:00",
            "paused_at": None,
            "paused_seconds": 0.0,
            "completed_at": None,
            "recorded_seconds": 0.0,
            "task_id": None,
            "paper_id": None,
        }
    )

    with _paired_auth_patch(42):
        await focus_command(update, context)

    mock_http.post.assert_awaited_once()
    headers = mock_http.post.await_args[1]["headers"]
    assert headers.get("X-Owner-User-Id") == "42", (
        f"focus start must send X-Owner-User-Id=42, headers={headers}"
    )
    assert "X-API-Key" not in headers
    assert mock_http.post.await_args.kwargs["json"] == {
        "duration_seconds": 1500,
        "source": "telegram",
    }
    context.job_queue.run_once.assert_not_called()


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
