"""Tests for Telegram bot command handlers.

Covers: /start, /help, /papers, /stats, /briefing, /projects, /tasks, /done, /newproject.
Each handler function is tested directly with mocked Update + Context objects.
"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from jarvis_common.testing import PTBContextOptions, make_bot_config, make_ptb_context
from jarvis_common.testing_telegram import make_http_response
from telegram_bot.config import BotConfig
from telegram_bot.focus_contract import FocusSession, FocusTransition
from telegram_bot.formatters import LISTING_ROWS
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
from telegram_bot.platform_client import TimerPreferences
from telegram_bot.services_client import MyDayFocusSummary

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


def _capture_scheduled(context) -> list:
    """Collect coroutines the handler schedules instead of awaiting inline.

    A handler must not await minutes of backend work inside the update loop:
    this application processes updates one at a time, so doing so would stop
    the bot answering anyone. Capturing the scheduled coroutine lets a test
    assert both that property and what the detached work eventually does.
    """
    scheduled: list = []
    context.application.create_task = scheduled.append
    return scheduled


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
    header = update.message.reply_text.call_args_list[0][0][0]
    assert "Library" in header
    assert "showing 1 of 1" in header
    text = update.message.reply_text.call_args_list[1][0][0]
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
    scheduled = _capture_scheduled(context)

    await discover_command(update, context)

    # The search runs detached: a source-wide search takes minutes, and an
    # inline await would stop the bot answering every other user meanwhile.
    assert mock_http.post.await_count == 0, "/discover must not search inside the update loop"
    ack = update.message.reply_text.await_args.args[0]
    assert "Searching" in ack, "/discover must say the search started"
    assert len(scheduled) == 1
    await scheduled[0]

    mock_http.post.assert_awaited_once()
    call = mock_http.post.await_args
    assert call.args[0].endswith("/api/search")
    body = call.kwargs["json"]
    assert body["query"] == "transformer"
    assert set(body["source_types"]) == {"arxiv", "semantic_scholar", "openalex", "pubmed"}
    # The backend worst case observed for this call is 70.5 s.
    assert call.kwargs["timeout"] > 70.5

    text = update.message.reply_text.await_args.args[0]
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
    scheduled = _capture_scheduled(context)

    await discover_command(update, context)
    await scheduled[0]

    text = update.message.reply_text.await_args.args[0]
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
    scheduled = _capture_scheduled(context)

    await discover_command(update, context)
    await scheduled[0]

    text = update.message.reply_text.await_args.args[0]
    assert "Found 2 papers, but none could be saved to your library." in text
    assert "saved them to your library" not in text


@pytest.mark.asyncio
async def test_discover_without_a_query_shows_usage():
    """/discover needs a query; it must not fire a source-wide search without one."""
    update, context, _, mock_http = _make_update_and_context(args=[])
    scheduled = _capture_scheduled(context)

    await discover_command(update, context)

    mock_http.post.assert_not_awaited()
    assert not scheduled, "the usage reply must not schedule a search either"
    text = update.message.reply_text.await_args.args[0]
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
    scheduled = _capture_scheduled(context)

    await discover_command(update, context)
    await scheduled[0]

    text = update.message.reply_text.await_args.args[0]
    assert "saved" not in text.lower()
    assert "obscure" in text
    assert "No external source returned a paper" in text


@pytest.mark.asyncio
async def test_discover_api_failure_sends_error():
    """/discover surfaces a backend failure instead of a silent empty result."""
    update, context, _, mock_http = _make_update_and_context(args=["transformer"])
    mock_http.post.side_effect = Exception("Connection failed")
    scheduled = _capture_scheduled(context)

    await discover_command(update, context)
    await scheduled[0]

    text = update.message.reply_text.await_args.args[0]
    assert "Discovery failed" in text


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
        make_http_response({"total": 42}),  # fetch_inbox_count → feed?view=inbox
        make_http_response({"due_now": 5}),  # fetch_due_card_count → stats
        make_http_response(  # fetch_tasks
            [
                {"title": "Task 1", "project_name": "Proj", "status": "blocked"},
                {"title": "Task 2", "project_name": "Proj", "status": "done"},
            ]
        ),
        make_http_response([]),  # fetch_upcoming_milestones
    ]
    scheduled = _capture_scheduled(context)

    await briefing_command(update, context)

    # The five-section gather runs detached: awaiting it here would stop the
    # bot answering every other user for as long as the backend takes.
    assert mock_http.get.await_count == 0, "/briefing must not gather inside the update loop"
    assert len(scheduled) == 1
    await scheduled[0]

    text = update.message.reply_text.await_args.args[0]
    assert "Briefing" in text or "briefing" in text.lower()
    # Each count names its window and its view.
    assert "3</b> papers added to your library since midnight UTC" in text
    assert "42</b> waiting in your inbox" in text
    assert "5</b> cards due for review right now" in text
    # A blocked task is not done; a done one is excluded, as in My Day.
    assert "Open tasks (1)" in text
    assert "Task 1" in text
    assert "Task 2" not in text


@pytest.mark.asyncio
async def test_briefing_failure_answers_instead_of_going_silent():
    """A failed gather must reach the user, who was told the briefing was coming."""
    update, context, _, mock_http = _make_update_and_context()
    mock_http.get.side_effect = Exception("Connection failed")
    scheduled = _capture_scheduled(context)

    await briefing_command(update, context)
    await scheduled[0]

    text = update.message.reply_text.await_args.args[0]
    assert "Could not put your briefing together" in text


@pytest.mark.asyncio
async def test_briefing_partial_degradation_on_milestones_failure():
    """R7: a failed individual gather leaves that section empty, not the whole briefing."""
    update, context, _, mock_http = _make_update_and_context()
    mock_http.get.side_effect = [
        make_http_response({"total": 1}),  # new-paper count OK
        make_http_response({"total": 4}),  # inbox count OK
        make_http_response({"due_now": 2}),  # due cards OK
        make_http_response(  # tasks OK
            [{"title": "Task 1", "project_name": "Proj", "status": "in_progress"}]
        ),
        make_http_response(None, status=500),  # milestones fail → 5xx
    ]
    scheduled = _capture_scheduled(context)

    await briefing_command(update, context)
    assert mock_http.get.await_count == 0, "/briefing must not gather inside the update loop"
    await scheduled[0]

    # Briefing still sends, with the working sections rendered.
    text = update.message.reply_text.await_args.args[0]
    assert "Briefing" in text or "briefing" in text.lower()
    assert "Task 1" in text
    # Each surviving count carries the value its own gather returned, so a
    # gather whose payload went to the wrong section cannot pass unnoticed.
    assert "1</b> papers added to your library since midnight UTC" in text
    assert "4</b> waiting in your inbox" in text
    assert "2</b> cards due for review right now" in text
    # The list section that failed says so. An absent section reads as "nothing
    # due", which is the same untruth as a zero count.
    assert "Milestones due in the next 7 days are unavailable right now" in text


@pytest.mark.asyncio
async def test_briefing_reports_unavailable_counts_rather_than_zero():
    """A count whose gather failed says so; a backend outage is not a real zero."""
    update, context, _, mock_http = _make_update_and_context()
    mock_http.get.side_effect = [
        make_http_response(None, status=500),  # new-paper count fails
        make_http_response(None, status=500),  # inbox count fails
        make_http_response(None, status=500),  # due cards fail
        make_http_response(  # tasks OK
            [{"title": "Task 1", "project_name": "Proj", "status": "todo"}]
        ),
        make_http_response([]),  # milestones OK
    ]
    scheduled = _capture_scheduled(context)

    await briefing_command(update, context)
    assert mock_http.get.await_count == 0, "/briefing must not gather inside the update loop"
    await scheduled[0]

    text = update.message.reply_text.await_args.args[0]
    assert "0</b> papers added to your library since midnight UTC" not in text
    assert "0</b> waiting in your inbox" not in text
    assert "0</b> cards due for review right now" not in text
    assert "Papers added to your library since midnight UTC are unavailable right now" in text
    assert "Your inbox count is unavailable right now" in text
    assert "Cards due for review are unavailable right now" in text
    # The sections that were read still render.
    assert "Task 1" in text
    # An empty list that was actually read stays absent; only an unread one speaks.
    assert "Milestones due in the next 7 days" not in text


@pytest.mark.asyncio
async def test_briefing_distinguishes_an_unread_list_from_an_empty_one():
    """An unread list says so; a list read as empty stays silent."""
    update, context, _, mock_http = _make_update_and_context()
    mock_http.get.side_effect = [
        make_http_response({"count": 1}),
        make_http_response({"total": 4}),
        make_http_response({"due_count": 2}),
        make_http_response(None, status=500),  # tasks unreadable
        make_http_response([]),  # milestones genuinely empty
    ]
    scheduled = _capture_scheduled(context)

    await briefing_command(update, context)
    await scheduled[0]

    text = update.message.reply_text.await_args.args[0]
    assert "Your open tasks are unavailable right now" in text
    assert "Open tasks (" not in text
    assert "Milestones due in the next 7 days" not in text


# ---------------------------------------------------------------------------
# Tests: /projects
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_projects_empty():
    """/projects with nothing to list says so and names the one exclusion."""
    update, context, _, mock_http = _make_update_and_context()
    mock_http.get.return_value = make_http_response([])
    await projects_command(update, context)
    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert "No projects yet" in text
    assert "archived" in text


@pytest.mark.asyncio
async def test_projects_lists_every_non_archived_project_with_its_label():
    """/projects lists paused and completed projects too, hiding only archived ones."""
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
            {
                "id": 2,
                "name": "Project Beta",
                "status": "paused",
                "description": None,
                "deadline": None,
            },
            {
                "id": 3,
                "name": "Project Gamma",
                "status": "archived",
                "description": None,
                "deadline": None,
            },
        ]
    )
    await projects_command(update, context)

    mock_http.get.assert_awaited_once()
    call = mock_http.get.await_args
    assert "/api/projects" in call.args[0]
    assert call.kwargs["params"] is None
    texts = [c[0][0] for c in update.message.reply_text.call_args_list]
    assert len(texts) == 3  # header, then one message per listed project
    assert "showing 2 of 2" in texts[0]
    # The stored status never reaches the user; the shared label does.
    assert "Project Alpha" in texts[1] and "In progress" in texts[1]
    assert "Project Beta" in texts[2] and "Draft" in texts[2]
    assert "paused" not in texts[2]
    assert not any("Project Gamma" in text for text in texts)


@pytest.mark.asyncio
async def test_projects_caps_the_listing_and_says_how_many_there_are():
    """A long project list stops at the cap and states the full count up front.

    One message per project means an uncapped list floods the chat and can be
    cut short by Telegram's throttling with nothing explaining the gap.
    """
    update, context, _, mock_http = _make_update_and_context()
    mock_http.get.return_value = make_http_response(
        [
            {"id": n, "name": f"Project {n}", "status": "active", "description": None}
            for n in range(1, 13)
        ]
    )

    await projects_command(update, context)

    texts = [c[0][0] for c in update.message.reply_text.call_args_list]
    assert len(texts) == LISTING_ROWS + 1  # the header plus the capped listing
    assert f"showing {LISTING_ROWS} of 12" in texts[0]
    assert "Project 10" in texts[LISTING_ROWS]
    assert not any("Project 11" in text or "Project 12" in text for text in texts)


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
    """/done without args prompts for usage and points at the command that lists ids."""
    update, context, _, _ = _make_update_and_context(args=[])
    await done_command(update, context)
    text = update.message.reply_text.call_args[0][0]
    assert "Usage" in text or "task_id" in text
    assert "/tasks" in text


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


@pytest.mark.asyncio
async def test_newproject_reply_failure_does_not_report_the_project_as_uncreated():
    """A confirmation that cannot be sent must not deny a project that exists.

    The POST has already created the project when the reply is attempted, so
    "Failed to create project" would send the user back to /newproject and
    leave them with two.
    """
    update, context, _, mock_http = _make_update_and_context(args=["My", "Project"])
    mock_http.post.return_value = make_http_response({"id": 42})
    update.message.reply_text.side_effect = RuntimeError("Flood control exceeded")

    with contextlib.suppress(RuntimeError):
        await newproject_command(update, context)

    mock_http.post.assert_awaited_once()  # the project was created
    texts = [c[0][0] for c in update.message.reply_text.call_args_list]
    assert len(texts) == 1
    assert "Failed to create project" not in texts[0]


@pytest.mark.asyncio
async def test_newproject_unexpected_response_shape_still_confirms_the_project() -> None:
    """A response without the identifier must not deny a project that exists.

    Reading the identifier used to sit inside the failure guard, so a backend
    that answered with a different key reported "Failed to create project" for
    a project the POST had already created.
    """
    update, context, _, mock_http = _make_update_and_context(args=["My", "Project"])
    mock_http.post.return_value = make_http_response({"project_id": 42})

    await newproject_command(update, context)

    mock_http.post.assert_awaited_once()  # the project was created
    text = update.message.reply_text.call_args[0][0]
    assert "Failed to create project" not in text
    assert "My Project" in text
    assert "created" in text


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


@pytest.mark.asyncio
async def test_post_shutdown_flushes_telemetry_even_when_resource_cleanup_fails() -> None:
    """A bot restart flushes telemetry but leaves the process provider alive."""
    from telegram_bot.main import post_shutdown  # noqa: PLC0415

    application = SimpleNamespace(
        bot_data={"scheduler": SimpleNamespace(stop=AsyncMock(side_effect=RuntimeError("stop")))}
    )
    with (
        patch(
            "telegram_bot.internal_api._server_state",
            SimpleNamespace(server=None, task=None),
        ),
        patch("telegram_bot.main.flush_telemetry") as flush_telemetry,
        pytest.raises(RuntimeError, match="stop"),
    ):
        await post_shutdown(application)

    flush_telemetry.assert_called_once_with()


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
    scheduled = _capture_scheduled(context)

    await discover_command(update, context)
    await scheduled[0]

    sent_query = mock_http.post.await_args.kwargs["json"]["query"]
    assert sent_query == "neuralnets"


# ---------------------------------------------------------------------------
# Per-user scoping of interactive commands
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_briefing_scopes_to_user_via_owner_header_when_paired():
    """A paired /briefing stages its user ID on every pre-auth request."""
    update, context, _, mock_http = _make_paired_update_and_context(jarvis_user_id=7)
    context.user_data["jarvis_user_id"] = 7
    mock_http.get.side_effect = [
        make_http_response({"total": 0}),  # new papers
        make_http_response({"total": 0}),  # inbox count
        make_http_response({"due_now": 0}),  # due cards
        make_http_response([]),  # tasks
        make_http_response([]),  # milestones
    ]
    scheduled = _capture_scheduled(context)

    with _paired_auth_patch(7):
        await briefing_command(update, context)
    await scheduled[0]

    # Every gather carries the owner header scoping the response to user 7.
    assert mock_http.get.await_count == 5
    for call in mock_http.get.await_args_list:
        assert call.kwargs["headers"].get("X-Jarvis-Paired-User-Id") == "7"


@pytest.mark.asyncio
async def test_briefing_unpaired_owner_gets_pairing_message():
    """S6: /briefing from an unpaired chat (auth_check -> (False, None)) is
    blocked with the /pair guidance reply."""
    update, context, _, mock_http = _make_update_and_context()
    update.message.reply_text = AsyncMock()
    scheduled = _capture_scheduled(context)

    with patch(
        "telegram_bot.handlers.commands._auth.auth_check",
        new_callable=AsyncMock,
        return_value=(False, None),
    ):
        await briefing_command(update, context)

    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert "pair" in text.lower() or "Settings" in text
    # No backend call for the briefing itself should have been issued, inline
    # or detached: an unpaired chat must not reach the backend at all.
    mock_http.get.assert_not_awaited()
    assert not scheduled


@pytest.mark.asyncio
async def test_tasks_scopes_query_to_paired_user():
    """A12: /tasks stages paired identity and the in-progress filter."""
    update, context, _, mock_http = _make_paired_update_and_context(jarvis_user_id=7, args=[])
    context.user_data["jarvis_user_id"] = 7
    mock_http.get.return_value = make_http_response([])

    with _paired_auth_patch(7):
        await tasks_command(update, context)

    call = mock_http.get.await_args
    assert "/api/tasks" in call.args[0]
    assert call.kwargs["headers"].get("X-Jarvis-Paired-User-Id") == "7"
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
    assert call.kwargs["headers"].get("X-Jarvis-Paired-User-Id") == "7"
    assert call.kwargs["params"]["status"] == "in_progress"
    assert call.kwargs["params"]["project_id"] == 3


@pytest.mark.asyncio
async def test_done_passes_user_id_via_owner_header():
    """/done stages the paired identity before its PUT assertion exchange."""
    update, context, _, mock_http = _make_paired_update_and_context(jarvis_user_id=7, args=["5"])
    context.user_data["jarvis_user_id"] = 7
    # 404 → unowned/nonexistent task → "not found"
    mock_http.put.return_value = make_http_response(None, status=404)

    with _paired_auth_patch(7):
        await done_command(update, context)

    mock_http.put.assert_awaited_once()
    call = mock_http.put.await_args
    assert "/api/tasks/5" in call.args[0]
    assert call.kwargs["headers"].get("X-Jarvis-Paired-User-Id") == "7"
    text = update.message.reply_text.call_args[0][0]
    assert "not found" in text.lower() or "5" in text


@pytest.mark.asyncio
async def test_projects_scopes_listing_to_paired_user():
    """A13: /projects stages paired identity on an unfiltered project listing."""
    update, context, _, mock_http = _make_paired_update_and_context(jarvis_user_id=7)
    context.user_data["jarvis_user_id"] = 7
    mock_http.get.return_value = make_http_response([])

    with _paired_auth_patch(7):
        await projects_command(update, context)

    call = mock_http.get.await_args
    assert "/api/projects" in call.args[0]
    assert call.kwargs["headers"].get("X-Jarvis-Paired-User-Id") == "7"
    assert call.kwargs["params"] is None


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
    """A paired user carries its private marker on POST /api/projects."""
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
    assert call.kwargs["headers"].get("X-Jarvis-Paired-User-Id") == "7"
    assert call.kwargs["json"]["name"] == "Alpha"


# ---------------------------------------------------------------------------
# Paired-user context staged by paper commands before assertion exchange
# ---------------------------------------------------------------------------


from telegram_bot.handlers.commands import system_commands  # noqa: E402
from telegram_bot.handlers.commands.paper_commands import (  # noqa: E402
    inbox_command,
    next_command,
)
from telegram_bot.handlers.commands.system_commands import pulse_now_command  # noqa: E402


@pytest.mark.asyncio
async def test_papers_command_sends_owner_user_id_for_paired_user():
    """/papers stages the paired user's local assertion marker."""
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
    assert headers.get("X-Jarvis-Paired-User-Id") == "7"
    assert "X-API-Key" not in headers


@pytest.mark.asyncio
async def test_stats_command_sends_owner_user_id_for_paired_user():
    """/stats stages the paired user's local assertion marker."""
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
    assert headers.get("X-Jarvis-Paired-User-Id") == "7"
    assert "X-API-Key" not in headers


@pytest.mark.asyncio
async def test_next_command_sends_owner_user_id_for_paired_user():
    """/next stages the paired user's local assertion marker."""
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
    assert headers.get("X-Jarvis-Paired-User-Id") == "7"


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
    """/inbox stages the paired user's local assertion marker."""
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
    assert headers.get("X-Jarvis-Paired-User-Id") == "7"


@pytest.mark.asyncio
async def test_pulse_now_command_sends_owner_user_id_for_paired_user():
    """/pulse_now stages the paired user's local assertion marker."""
    update, context, _, mock_http = _make_paired_update_and_context(jarvis_user_id=7)
    context.user_data["jarvis_user_id"] = 7
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_http.post.return_value = mock_resp

    with _paired_auth_patch(7):
        await pulse_now_command(update, context)

    mock_http.post.assert_awaited_once()
    headers = mock_http.post.await_args[1]["headers"]
    assert headers.get("X-Jarvis-Paired-User-Id") == "7"


def _pulse_deck_payload(states: list[str | None]) -> dict:
    """Build a deck payload whose cards carry the given per-user states."""
    return {
        "deck_id": 7,
        "deck_date": "2026-08-07",
        "card_count": len(states),
        "generated_at": "2026-08-07T06:00:00+00:00",
        "cards": [
            {
                "card_id": 100 + index,
                "paper_id": 200 + index,
                "paper_title": f"Paper {index}",
                "paper_authors": ["A researcher"],
                "paper_url": "https://example.org/paper",
                "rank": index + 1,
                "score": 0.9 - 0.1 * index,
                "llm_relevance": 8,
                "llm_novelty": 7,
                "reasoning": "Relevant to the configured topic.",
                "signals": {"recency": 0.5},
                "user_state": state,
            }
            for index, state in enumerate(states)
        ],
        "stats": {},
    }


@pytest.mark.asyncio
async def test_next_command_skips_cards_the_user_already_acted_on():
    """/next advances past acted cards to the highest-ranked untouched one."""
    update, context, _, mock_http = _make_paired_update_and_context(jarvis_user_id=7)
    context.user_data["jarvis_user_id"] = 7
    # Rank 1 is saved and rank 2 has no state row at all, so rank 2 is next.
    mock_http.get.return_value = make_http_response(_pulse_deck_payload(["to_read", None, "done"]))

    with _paired_auth_patch(7):
        await next_command(update, context)

    text = update.message.reply_text.await_args.args[0]
    assert "Paper 1" in text
    assert "Paper 0" not in text
    keyboard = update.message.reply_text.await_args.kwargs["reply_markup"]
    callbacks = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert all("201" in data for data in callbacks)


@pytest.mark.asyncio
async def test_next_command_reports_a_deck_the_user_finished():
    """/next says the deck is exhausted instead of resurfacing an acted card."""
    update, context, _, mock_http = _make_paired_update_and_context(jarvis_user_id=7)
    context.user_data["jarvis_user_id"] = 7
    context.application.bot_data["config"] = make_bot_config(
        BotConfig, jarvis_base_url="https://jarvis.example.test"
    )
    mock_http.get.return_value = make_http_response(_pulse_deck_payload(["to_read", "trash"]))

    with _paired_auth_patch(7):
        await next_command(update, context)

    text = update.message.reply_text.await_args.args[0]
    assert "acted on all 2 Pulse cards" in text
    assert 'href="https://jarvis.example.test/pulse"' in text


@pytest.mark.asyncio
async def test_pulse_now_command_delivers_the_deck_once_the_job_succeeds(monkeypatch):
    """/pulse_now waits for the job it started, then uses the scheduled delivery path."""
    monkeypatch.setattr(system_commands, "_PULSE_POLL_INTERVAL_SECONDS", 0.0)
    update, context, _, mock_http = _make_paired_update_and_context(jarvis_user_id=7)
    context.user_data["jarvis_user_id"] = 7
    mock_http.post.return_value = make_http_response({"job_id": "job-1", "status": "queued"})
    mock_http.get.side_effect = [
        make_http_response({"job_id": "job-1", "status": "running"}),
        make_http_response({"job_id": "job-1", "status": "succeeded"}),
    ]
    deliver = AsyncMock()
    monkeypatch.setattr(system_commands, "deliver_pulse_to_chat", deliver)
    scheduled = _capture_scheduled(context)

    with _paired_auth_patch(7):
        await pulse_now_command(update, context)

    assert mock_http.get.await_count == 0, "the handler must not poll the job inline"
    assert len(scheduled) == 1
    await scheduled[0]

    assert mock_http.get.await_count == 2
    deliver.assert_awaited_once()
    assert deliver.await_args.args[3] == 99999
    assert deliver.await_args.args[4] == 7


@pytest.mark.asyncio
async def test_pulse_now_command_reports_a_job_that_outlives_the_wait(monkeypatch):
    """/pulse_now stops waiting at its budget and says so instead of delivering."""
    monkeypatch.setattr(system_commands, "_PULSE_POLL_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(system_commands, "_PULSE_POLL_BUDGET_SECONDS", 0.0)
    update, context, _, mock_http = _make_paired_update_and_context(jarvis_user_id=7)
    context.user_data["jarvis_user_id"] = 7
    mock_http.post.return_value = make_http_response({"job_id": "job-1", "status": "queued"})
    mock_http.get.return_value = make_http_response({"job_id": "job-1", "status": "running"})
    deliver = AsyncMock()
    monkeypatch.setattr(system_commands, "deliver_pulse_to_chat", deliver)
    context.bot.send_message = AsyncMock()
    scheduled = _capture_scheduled(context)

    with _paired_auth_patch(7):
        await pulse_now_command(update, context)

    assert len(scheduled) == 1
    await scheduled[0]

    deliver.assert_not_awaited()
    sent = [call.kwargs["text"] for call in context.bot.send_message.await_args_list]
    assert "still generating" in sent[-1]


@pytest.mark.asyncio
async def test_briefing_command_sends_owner_user_id_to_stats_endpoint():
    """/briefing sends only the paired-user marker to the stats client."""
    update, context, _, mock_http = _make_paired_update_and_context(jarvis_user_id=7)
    context.user_data["jarvis_user_id"] = 7
    mock_http.get.side_effect = [
        make_http_response({"total": 0}),  # new papers
        make_http_response({"total": 0}),  # inbox count
        make_http_response({"due_now": 0}),  # due cards → /api/stats
        make_http_response([]),  # tasks
        make_http_response([]),  # milestones
    ]
    scheduled = _capture_scheduled(context)

    with _paired_auth_patch(7):
        await briefing_command(update, context)
    await scheduled[0]

    # Locate the /api/stats GET and verify the local assertion marker.
    stats_calls = [c for c in mock_http.get.await_args_list if c.args[0].endswith("/api/stats")]
    assert stats_calls, "briefing must call /api/stats for due-card count"
    headers = stats_calls[0].kwargs["headers"]
    assert headers.get("X-Jarvis-Paired-User-Id") == "7"
    assert "X-API-Key" not in headers


# ---------------------------------------------------------------------------
# focus_alarm stages paired-user context for assertion exchange
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
    assert headers.get("X-Jarvis-Paired-User-Id") == "42", (
        f"focus start must stage paired user 42, headers={headers}"
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


# ---------------------------------------------------------------------------
# Tests: /focus — status, saved duration, and sub-commands
# ---------------------------------------------------------------------------

_FOCUS_MODULE = "telegram_bot.handlers.commands.system_commands"
_PREFERENCES = f"{_FOCUS_MODULE}.platform_client.get_timer_preferences"
_ACTIVE_SESSION = f"{_FOCUS_MODULE}.services_client.fetch_active_focus_session"
_MY_DAY = f"{_FOCUS_MODULE}.services_client.fetch_my_day_focus"
_START_SESSION = f"{_FOCUS_MODULE}.services_client.start_focus_session"


def _focus_session(**overrides) -> FocusSession:
    """Build a server-shaped focus session, overriding only what a test cares about."""
    fields = {
        "id": 5,
        "state": "active",
        "source": "telegram",
        "duration_seconds": 1500,
        "remaining_seconds": 600,
        "started_at": "2026-08-19T10:00:00+00:00",
        "paused_at": None,
        "paused_seconds": 0.0,
        "completed_at": None,
        "recorded_seconds": 0.0,
        "task_id": None,
        "paper_id": None,
    }
    fields.update(overrides)
    return FocusSession(**fields)


@pytest.mark.asyncio
async def test_focus_without_args_reports_status_instead_of_starting():
    """/focus reports state, the day's total against the target, streak, and sub-commands."""
    update, context, _, _ = _make_focus_update_and_context(args=[])

    with (
        patch(_ACTIVE_SESSION, new_callable=AsyncMock, return_value=_focus_session()),
        patch(_PREFERENCES, new_callable=AsyncMock, return_value=TimerPreferences(45, 4)),
        patch(
            _MY_DAY,
            new_callable=AsyncMock,
            return_value=MyDayFocusSummary(today_focus_hours=1.5, focus_streak_days=3),
        ),
        patch(_START_SESSION, new_callable=AsyncMock) as start_focus,
    ):
        await focus_command(update, context)

    start_focus.assert_not_awaited()
    text = update.message.reply_text.call_args[0][0]
    assert "Focus running — 10 min left." in text
    assert "90 of 180 target minutes" in text
    assert "Streak: 3 days" in text
    assert "/focus pause" in text


@pytest.mark.asyncio
async def test_focus_start_without_minutes_uses_the_saved_duration():
    """/focus start takes its length from the user's saved preference, not a fixed 25."""
    update, context, _, _ = _make_focus_update_and_context(args=["start"])

    with (
        patch(_PREFERENCES, new_callable=AsyncMock, return_value=TimerPreferences(45, 4)),
        patch(_START_SESSION, new_callable=AsyncMock) as start_focus,
    ):
        await focus_command(update, context)

    assert start_focus.await_args.args[2:] == (1, 2700)
    assert "45" in update.message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_focus_start_says_when_the_saved_duration_was_unreachable():
    """An unreadable preference falls back to 25 minutes and the reply admits it."""
    update, context, _, _ = _make_focus_update_and_context(args=["start"])

    with (
        patch(
            _PREFERENCES,
            new_callable=AsyncMock,
            side_effect=httpx.ConnectError("platform unreachable"),
        ),
        patch(_START_SESSION, new_callable=AsyncMock) as start_focus,
    ):
        await focus_command(update, context)

    assert start_focus.await_args.args[2:] == (1, 1500)
    assert "unreachable" in update.message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_focus_pause_without_a_session_says_so():
    """/focus pause with nothing running says so instead of reporting a pause."""
    update, context, _, _ = _make_focus_update_and_context(args=["pause"])

    with (
        patch(_ACTIVE_SESSION, new_callable=AsyncMock, return_value=None),
        patch(f"{_FOCUS_MODULE}.services_client.pause_focus_session", new_callable=AsyncMock) as p,
    ):
        await focus_command(update, context)

    p.assert_not_awaited()
    assert "No focus session is running." in update.message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_focus_stop_completes_the_active_session():
    """/focus stop completes the open interval and reports the recorded time."""
    update, context, _, _ = _make_focus_update_and_context(args=["stop"])
    stopped = _focus_session(
        state="completed",
        remaining_seconds=0,
        completed_at="2026-08-19T10:25:00+00:00",
        recorded_seconds=1500.0,
    )

    with (
        patch(_ACTIVE_SESSION, new_callable=AsyncMock, return_value=_focus_session()),
        patch(
            f"{_FOCUS_MODULE}.services_client.complete_focus_session",
            new_callable=AsyncMock,
            return_value=FocusTransition(session=stopped, changed=True),
        ) as complete,
    ):
        await focus_command(update, context)

    assert complete.await_args.args[2:] == (1, 5, "stop")
    assert "Focus stopped — 25 minutes recorded." in update.message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_focus_start_conflict_points_at_the_bot_not_the_web_timer():
    """A 409 sends the user to /focus, which can now show and end the session."""
    update, context, _, _ = _make_focus_update_and_context(args=["30"])
    request = httpx.Request("POST", "http://learn:8001/api/executive/focus/start")
    conflict = httpx.HTTPStatusError(
        "conflict", request=request, response=httpx.Response(409, request=request)
    )

    with patch(_START_SESSION, new_callable=AsyncMock, side_effect=conflict):
        await focus_command(update, context)

    text = update.message.reply_text.call_args[0][0]
    assert "/focus stop" in text
    assert "Web timer" not in text


@pytest.mark.asyncio
async def test_tasks_rows_offer_a_mark_done_button_each():
    """Every listed task carries the registered task_done_<id> button."""
    update, context, _, mock_http = _make_update_and_context(args=[])
    mock_http.get.return_value = make_http_response(
        [
            {"id": 1, "title": "Fix bug", "status": "in_progress", "project_name": None},
            {"id": 7, "title": "Write tests", "status": "in_progress", "project_name": None},
        ]
    )

    await tasks_command(update, context)

    markup = update.message.reply_text.call_args.kwargs["reply_markup"]
    assert [row[0].callback_data for row in markup.inline_keyboard] == [
        "task_done_1",
        "task_done_7",
    ]


@pytest.mark.asyncio
async def test_plain_text_gets_an_answer_rather_than_silence():
    """A non-command message is answered with the one thing the bot understands."""
    from telegram_bot.main import _unrecognized_text

    update, context, _, _ = _make_update_and_context()

    await _unrecognized_text(update, context)

    assert update.message.reply_text.call_args[0][0] == "I only understand commands — try /help"


def test_main_registers_the_text_catch_all_after_every_other_handler():
    """The catch-all is last in the default group, so it never pre-empts a real handler."""
    from telegram.ext import MessageHandler

    with (
        patch("telegram_bot.main.BotConfig.from_env", return_value=MagicMock()),
        patch("telegram_bot.main.Application.builder") as builder,
    ):
        app = MagicMock()
        for step in ("request", "get_updates_request", "token", "post_init", "post_shutdown"):
            getattr(builder.return_value, step).return_value = builder.return_value
        builder.return_value.build.return_value = app

        from telegram_bot.main import _unrecognized_text, main

        main()

    default_group = [c for c in app.add_handler.call_args_list if "group" not in c.kwargs]
    last = default_group[-1].args[0]
    assert isinstance(last, MessageHandler)
    assert last.callback is _unrecognized_text
