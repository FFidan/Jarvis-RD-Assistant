"""Tests for Telegram bot inline-keyboard callback handlers.

Covers: paper_detail, paper_action (9 lifecycle actions), paper_feedback,
project_detail, task_done, start_review.  Legacy bookmark/dismiss/save tests
replaced by the new dispatcher pattern (T1).

H1 invariant: every test for the dispatcher callbacks asserts
``query.answer.call_count == 1`` on every execution path.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import telegram
import telegram_bot.handlers.review_handler as _review_handler_mod
from jarvis_common.testing import make_bot_config
from jarvis_common.testing_telegram import make_http_response
from telegram import Message, Update
from telegram.ext import ContextTypes
from telegram_bot.config import BotConfig
from telegram_bot.handlers import rate_limit as _rate_limit_mod
from telegram_bot.handlers.callback_handler import (
    _callback_auth,
    paper_action_callback,
    paper_detail_callback,
    paper_feedback_callback,
    project_detail_callback,
    task_done_callback,
)
from telegram_bot.handlers.rate_limit import rate_limit

# ---------------------------------------------------------------------------
# Test-only scaffolding: start_review_callback
#
# This function formerly lived in callback_handler.py as
# _test_only_start_review_callback.  It is NOT registered with the dispatcher
# (TG-003 — the ConversationHandler owns the /review flow); it exists here so
# the auth + rate-limit + ack pattern can be regression-tested without
# depending on the ConversationHandler.
# ---------------------------------------------------------------------------


@rate_limit(max_calls=5, window_seconds=60)
async def start_review_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Test-only scaffolding for start_review callback auth + ack pattern."""
    query = update.callback_query
    if query is None:
        return

    authorized, _ = await _callback_auth(update, context)
    if not authorized:
        await query.answer()  # H1: ack even on auth failure so Telegram stops the spinner
        return

    # Guard against InaccessibleMessage — can arrive when the message is older
    # than 48 hours.  A bare assignment silently casts the wrong type; instead
    # we answer with an alert so the user gets feedback.
    if not isinstance(query.message, Message):
        await query.answer("This message is no longer accessible", show_alert=True)
        return

    await query.answer()

    # Delegate to review_start, passing the callback message explicitly so that
    # update.message is never mutated (Update fields are conceptually immutable
    # per call and the assignment was fragile / type-unsafe).
    # Look up via module reference so patch("...review_handler.review_start") works.
    await _review_handler_mod.review_start(update, context, message=query.message)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_rate_limit_state():  # pyright: ignore[reportUnusedFunction]
    """Clear the rate-limiter's in-memory timestamp store before every test.

    The rate_limit decorator uses a module-level defaultdict keyed by
    ``chat_id:func_name``.  Without this fixture, tests that share chat_id
    12345 and the same handler accumulate timestamps and trip the limit.
    """
    _rate_limit_mod._timestamps.clear()
    yield
    _rate_limit_mod._timestamps.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TEST_CHAT_ID = 12345


def _make_callback_update_and_context(callback_data: str, chat_id: int = _TEST_CHAT_ID):
    """Build (Update, Context, mock_db, mock_http) tuple for callback tests."""
    update = MagicMock()
    update.effective_chat = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_chat.type = "private"

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


@pytest.mark.asyncio
async def test_paper_detail_callback_includes_api_key_header():
    """H7: paper_detail_callback passes X-API-Key header to the GET request."""
    update, context, _, mock_http = _make_callback_update_and_context("paper_detail_99")
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {
        "paper": {"title": "Test Paper", "authors": [], "published_date": None, "url": None},
        "summary": None,
    }
    mock_http.get.return_value = mock_resp

    await paper_detail_callback(update, context)

    mock_http.get.assert_awaited_once()
    call_kwargs = mock_http.get.await_args[1]
    headers = call_kwargs["headers"]
    assert headers.get("X-API-Key") == "test-key"


# ---------------------------------------------------------------------------
# Tests: paper_action_callback — happy paths (9 lifecycle actions)
# ---------------------------------------------------------------------------


def _make_action_mock_http() -> AsyncMock:
    """Return a mock_http whose .request() returns a successful response."""
    mock_http = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_http.request.return_value = mock_resp
    return mock_http


@pytest.mark.asyncio
async def test_paper_action_save():
    """paper_action_callback: save action — PUT /save, single answer '💾 Saved', H1."""
    update, context, _, _ = _make_callback_update_and_context("paper:save:42")
    mock_http = _make_action_mock_http()
    context.application.bot_data["http_client"] = mock_http

    await paper_action_callback(update, context)

    query = update.callback_query
    assert query.answer.call_count == 1  # H1
    assert query.answer.await_args[1].get("text") == "💾 Saved"
    query.message.reply_text.assert_awaited_once()
    reply_text = query.message.reply_text.call_args[0][0]
    assert "42" in reply_text and "💾 Saved" in reply_text
    # Correct HTTP call: PUT .../42/save
    mock_http.request.assert_awaited_once()
    call_args = mock_http.request.await_args
    assert call_args[0][0] == "PUT"
    assert "/api/papers/42/save" in call_args[0][1]


@pytest.mark.asyncio
async def test_paper_action_skip():
    """paper_action_callback: skip action — PUT /skip, single answer '⏩ Skipped', H1."""
    update, context, _, _ = _make_callback_update_and_context("paper:skip:7")
    mock_http = _make_action_mock_http()
    context.application.bot_data["http_client"] = mock_http

    await paper_action_callback(update, context)

    query = update.callback_query
    assert query.answer.call_count == 1  # H1
    assert query.answer.await_args[1].get("text") == "⏩ Skipped"
    query.message.reply_text.assert_awaited_once()
    mock_http.request.assert_awaited_once()
    call_args = mock_http.request.await_args
    assert call_args[0][0] == "PUT"
    assert "/api/papers/7/skip" in call_args[0][1]


@pytest.mark.asyncio
async def test_paper_action_reading():
    """paper_action_callback: reading — PUT /reading, single answer '📖 Marked Reading', H1."""
    update, context, _, _ = _make_callback_update_and_context("paper:reading:10")
    mock_http = _make_action_mock_http()
    context.application.bot_data["http_client"] = mock_http

    await paper_action_callback(update, context)

    query = update.callback_query
    assert query.answer.call_count == 1  # H1
    assert query.answer.await_args[1].get("text") == "📖 Marked Reading"
    mock_http.request.assert_awaited_once()
    call_args = mock_http.request.await_args
    assert call_args[0][0] == "PUT"
    assert "/api/papers/10/reading" in call_args[0][1]


@pytest.mark.asyncio
async def test_paper_action_done():
    """paper_action_callback: done action — PUT /done, single answer '✓ Marked Done', H1."""
    update, context, _, _ = _make_callback_update_and_context("paper:done:5")
    mock_http = _make_action_mock_http()
    context.application.bot_data["http_client"] = mock_http

    await paper_action_callback(update, context)

    query = update.callback_query
    assert query.answer.call_count == 1  # H1
    assert query.answer.await_args[1].get("text") == "✓ Marked Done"
    mock_http.request.assert_awaited_once()
    call_args = mock_http.request.await_args
    assert call_args[0][0] == "PUT"
    assert "/api/papers/5/done" in call_args[0][1]


@pytest.mark.asyncio
async def test_paper_action_trash():
    """paper_action_callback: trash action — PUT /trash, single answer '🗑 Trashed', H1."""
    update, context, _, _ = _make_callback_update_and_context("paper:trash:99")
    mock_http = _make_action_mock_http()
    context.application.bot_data["http_client"] = mock_http

    await paper_action_callback(update, context)

    query = update.callback_query
    assert query.answer.call_count == 1  # H1
    assert query.answer.await_args[1].get("text") == "🗑 Trashed"
    query.message.reply_text.assert_awaited_once()
    mock_http.request.assert_awaited_once()
    call_args = mock_http.request.await_args
    assert call_args[0][0] == "PUT"
    assert "/api/papers/99/trash" in call_args[0][1]


@pytest.mark.asyncio
async def test_paper_action_restore():
    """paper_action_callback: restore action — PUT /restore, single answer '↩ Restored', H1."""
    update, context, _, _ = _make_callback_update_and_context("paper:restore:3")
    mock_http = _make_action_mock_http()
    context.application.bot_data["http_client"] = mock_http

    await paper_action_callback(update, context)

    query = update.callback_query
    assert query.answer.call_count == 1  # H1
    assert query.answer.await_args[1].get("text") == "↩ Restored"
    mock_http.request.assert_awaited_once()
    call_args = mock_http.request.await_args
    assert call_args[0][0] == "PUT"
    assert "/api/papers/3/restore" in call_args[0][1]


@pytest.mark.asyncio
async def test_paper_action_trash_reject():
    """paper_action_callback: trash_reject — PUT /trash_and_reject (suffix differs), H1."""
    update, context, _, _ = _make_callback_update_and_context("paper:trash_reject:21")
    mock_http = _make_action_mock_http()
    context.application.bot_data["http_client"] = mock_http

    await paper_action_callback(update, context)

    query = update.callback_query
    assert query.answer.call_count == 1  # H1
    assert query.answer.await_args[1].get("text") == "🗑+👎 Trashed & Rejected"
    mock_http.request.assert_awaited_once()
    call_args = mock_http.request.await_args
    assert call_args[0][0] == "PUT"
    # URL suffix is trash_and_reject, NOT trash_reject
    assert "/api/papers/21/trash_and_reject" in call_args[0][1]


@pytest.mark.asyncio
async def test_paper_action_star():
    """paper_action_callback: star action — PUT /star, single answer '⭐ Starred', H1."""
    update, context, _, _ = _make_callback_update_and_context("paper:star:8")
    mock_http = _make_action_mock_http()
    context.application.bot_data["http_client"] = mock_http

    await paper_action_callback(update, context)

    query = update.callback_query
    assert query.answer.call_count == 1  # H1
    assert query.answer.await_args[1].get("text") == "⭐ Starred"
    mock_http.request.assert_awaited_once()
    call_args = mock_http.request.await_args
    assert call_args[0][0] == "PUT"
    assert "/api/papers/8/star" in call_args[0][1]


@pytest.mark.asyncio
async def test_paper_action_unstar():
    """paper_action_callback: unstar action — PUT /unstar, single answer '☆ Unstarred', H1."""
    update, context, _, _ = _make_callback_update_and_context("paper:unstar:15")
    mock_http = _make_action_mock_http()
    context.application.bot_data["http_client"] = mock_http

    await paper_action_callback(update, context)

    query = update.callback_query
    assert query.answer.call_count == 1  # H1
    assert query.answer.await_args[1].get("text") == "☆ Unstarred"
    mock_http.request.assert_awaited_once()
    call_args = mock_http.request.await_args
    assert call_args[0][0] == "PUT"
    assert "/api/papers/15/unstar" in call_args[0][1]


# ---------------------------------------------------------------------------
# Tests: paper_action_callback — failure paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_paper_action_failure_save():
    """On HTTP failure, query.answer carries error text; NO reply_text; H1."""
    update, context, _, _ = _make_callback_update_and_context("paper:save:42")
    mock_http = AsyncMock()
    mock_http.request.side_effect = Exception("Connection refused")
    context.application.bot_data["http_client"] = mock_http

    await paper_action_callback(update, context)

    query = update.callback_query
    assert query.answer.call_count == 1  # H1
    assert query.answer.await_args[1].get("text") == "save failed — try again later"
    # No reply_text on failure path
    query.message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_paper_action_failure_trash():
    """On HTTP failure for trash, error text in answer; no reply_text; H1."""
    update, context, _, _ = _make_callback_update_and_context("paper:trash:99")
    mock_http = AsyncMock()
    mock_http.request.side_effect = Exception("timeout")
    context.application.bot_data["http_client"] = mock_http

    await paper_action_callback(update, context)

    query = update.callback_query
    assert query.answer.call_count == 1  # H1
    assert query.answer.await_args[1].get("text") == "trash failed — try again later"
    query.message.reply_text.assert_not_awaited()


# ---------------------------------------------------------------------------
# Tests: paper_action_callback — bad data path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_paper_action_invalid_callback_data():
    """Invalid action name — query.answer called once (bare), no HTTP call; H1."""
    update, context, _, _ = _make_callback_update_and_context("paper:invalid_action:42")
    mock_http = AsyncMock()
    context.application.bot_data["http_client"] = mock_http

    await paper_action_callback(update, context)

    query = update.callback_query
    assert query.answer.call_count == 1  # H1 — bare answer on bad data
    mock_http.request.assert_not_awaited()


@pytest.mark.asyncio
async def test_paper_action_auth_fail_answers_query():
    """Regression guard: an unauthorised callback still answers the query.

    Without this, the Telegram client spins indefinitely on auth-rejected
    callbacks.
    """
    # Use a chat_id that does NOT match make_bot_config(BotConfig, ).telegram_chat_id so
    # auth_check returns False against both the env path and the DB path.
    update, context, mock_db, mock_http = _make_callback_update_and_context(
        "paper:save:42", chat_id=99999
    )
    # auth_check's DB fallback queries user_config; return None to take the
    # explicit "no owner paired" reject path.  Also return None for the
    # telegram_user_pairings lookup (migration 071) so the denial is complete.
    mock_db.fetchval.return_value = None
    mock_db.fetchrow.return_value = None

    await paper_action_callback(update, context)

    query = update.callback_query
    assert query.answer.call_count == 1  # H1 — auth-fail still answers
    mock_http.request.assert_not_awaited()


@pytest.mark.asyncio
async def test_paper_feedback_auth_fail_answers_query():
    """Regression guard for paper_feedback_callback."""
    update, context, mock_db, mock_http = _make_callback_update_and_context(
        "paper:feedback_pos:42:pulse_thumbs", chat_id=99999
    )
    mock_db.fetchval.return_value = None
    mock_db.fetchrow.return_value = None

    await paper_feedback_callback(update, context)

    query = update.callback_query
    assert query.answer.call_count == 1
    mock_http.post.assert_not_awaited()


# ---------------------------------------------------------------------------
# Tests: paper_feedback_callback — happy paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_paper_feedback_positive_pulse_thumbs():
    """Positive feedback via pulse_thumbs — POST /feedback, '👍 Recorded', H1."""
    update, context, _, _ = _make_callback_update_and_context("paper:feedback_pos:42:pulse_thumbs")
    mock_http = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_http.post.return_value = mock_resp
    context.application.bot_data["http_client"] = mock_http

    await paper_feedback_callback(update, context)

    query = update.callback_query
    assert query.answer.call_count == 1  # H1
    assert query.answer.await_args[1].get("text") == "👍 Recorded"
    mock_http.post.assert_awaited_once()
    call_args = mock_http.post.await_args
    assert "/api/papers/42/feedback" in call_args[0][0]
    assert call_args[1]["json"] == {"signal": "positive", "source": "pulse_thumbs"}


@pytest.mark.asyncio
async def test_paper_feedback_negative_pulse_thumbs():
    """Negative feedback via pulse_thumbs — POST /feedback, '👎 Recorded', H1."""
    update, context, _, _ = _make_callback_update_and_context("paper:feedback_neg:42:pulse_thumbs")
    mock_http = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_http.post.return_value = mock_resp
    context.application.bot_data["http_client"] = mock_http

    await paper_feedback_callback(update, context)

    query = update.callback_query
    assert query.answer.call_count == 1  # H1
    assert query.answer.await_args[1].get("text") == "👎 Recorded"
    mock_http.post.assert_awaited_once()
    call_args = mock_http.post.await_args
    assert "/api/papers/42/feedback" in call_args[0][0]
    assert call_args[1]["json"] == {"signal": "negative", "source": "pulse_thumbs"}


@pytest.mark.asyncio
async def test_paper_feedback_feed_thumbs_accepted():
    """feed_thumbs source value is accepted by the regex and processed without error; H1."""
    update, context, _, _ = _make_callback_update_and_context("paper:feedback_pos:10:feed_thumbs")
    mock_http = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_http.post.return_value = mock_resp
    context.application.bot_data["http_client"] = mock_http

    await paper_feedback_callback(update, context)

    query = update.callback_query
    assert query.answer.call_count == 1  # H1
    assert query.answer.await_args[1].get("text") == "👍 Recorded"
    call_args = mock_http.post.await_args
    assert call_args[1]["json"]["source"] == "feed_thumbs"


@pytest.mark.asyncio
async def test_paper_feedback_paper_detail_thumbs_accepted():
    """paper_detail_thumbs source value is accepted by the regex and processed; H1."""
    update, context, _, _ = _make_callback_update_and_context(
        "paper:feedback_neg:33:paper_detail_thumbs"
    )
    mock_http = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_http.post.return_value = mock_resp
    context.application.bot_data["http_client"] = mock_http

    await paper_feedback_callback(update, context)

    query = update.callback_query
    assert query.answer.call_count == 1  # H1
    assert query.answer.await_args[1].get("text") == "👎 Recorded"
    call_args = mock_http.post.await_args
    assert call_args[1]["json"]["source"] == "paper_detail_thumbs"


@pytest.mark.asyncio
async def test_paper_feedback_dismiss_combined_accepted():
    """dismiss_combined source value is accepted by the regex and processed; H1."""
    update, context, _, _ = _make_callback_update_and_context(
        "paper:feedback_neg:77:dismiss_combined"
    )
    mock_http = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_http.post.return_value = mock_resp
    context.application.bot_data["http_client"] = mock_http

    await paper_feedback_callback(update, context)

    query = update.callback_query
    assert query.answer.call_count == 1  # H1
    call_args = mock_http.post.await_args
    assert call_args[1]["json"]["source"] == "dismiss_combined"


# ---------------------------------------------------------------------------
# Tests: paper_feedback_callback — failure path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_paper_feedback_failure():
    """On HTTP failure, query.answer carries 'Feedback failed' text; H1."""
    update, context, _, _ = _make_callback_update_and_context("paper:feedback_pos:42:pulse_thumbs")
    mock_http = AsyncMock()
    mock_http.post.side_effect = Exception("Connection refused")
    context.application.bot_data["http_client"] = mock_http

    await paper_feedback_callback(update, context)

    query = update.callback_query
    assert query.answer.call_count == 1  # H1
    assert query.answer.await_args[1].get("text") == "Feedback failed — try again later"


# ---------------------------------------------------------------------------
# Tests: paper_feedback_callback — bad data path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_paper_feedback_invalid_callback_data():
    """Invalid source value — regex rejects, query.answer called once bare; H1."""
    update, context, _, _ = _make_callback_update_and_context(
        "paper:feedback_pos:42:unknown_source"
    )
    mock_http = AsyncMock()
    context.application.bot_data["http_client"] = mock_http

    await paper_feedback_callback(update, context)

    query = update.callback_query
    assert query.answer.call_count == 1  # H1 — bare answer on bad data
    mock_http.post.assert_not_awaited()


# ---------------------------------------------------------------------------
# Tests: project_detail
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_project_detail_success():
    """project_detail callback shows project status (project/tasks/milestones via REST)."""
    update, context, _, mock_http = _make_callback_update_and_context("project_detail_3")
    mock_http.get.side_effect = [
        make_http_response(
            {
                "id": 3,
                "name": "My Project",
                "status": "active",
                "description": "A project",
                "deadline": None,
            }
        ),  # fetch_project
        make_http_response([{"id": 1, "title": "Task A", "status": "in_progress"}]),  # tasks
        make_http_response(
            [{"id": 1, "name": "Milestone 1", "deadline": None, "completed": False}]
        ),  # milestones
    ]

    with patch(
        "telegram_bot.handlers.callback_handler.auth_check",
        new_callable=AsyncMock,
        return_value=(True, _PAIRED_USER_ID),
    ):
        await project_detail_callback(update, context)

    update.callback_query.answer.assert_awaited_once()
    update.callback_query.message.reply_text.assert_awaited_once()
    text = update.callback_query.message.reply_text.call_args[0][0]
    assert "My Project" in text

    # The three GETs hit project, tasks, milestones — all forwarding owner identity.
    assert mock_http.get.await_count == 3
    urls = [c.args[0] for c in mock_http.get.await_args_list]
    assert urls[0].endswith("/api/projects/3")
    assert urls[1].endswith("/api/projects/3/tasks")
    assert urls[2].endswith("/api/projects/3/milestones")
    for c in mock_http.get.await_args_list:
        assert c.kwargs["headers"].get("X-Owner-User-Id") == str(_PAIRED_USER_ID)


@pytest.mark.asyncio
async def test_project_detail_not_found():
    """project_detail callback sends 'not found' when project does not exist (404)."""
    update, context, _, mock_http = _make_callback_update_and_context("project_detail_999")
    mock_http.get.return_value = make_http_response(None, status=404)  # fetch_project → None

    with patch(
        "telegram_bot.handlers.callback_handler.auth_check",
        new_callable=AsyncMock,
        return_value=(True, _PAIRED_USER_ID),
    ):
        await project_detail_callback(update, context)

    update.callback_query.answer.assert_awaited_once()
    text = update.callback_query.message.reply_text.call_args[0][0]
    assert "not found" in text.lower()


@pytest.mark.asyncio
async def test_project_detail_service_error_replies_gracefully():
    """R7: a 5xx/timeout from any of the 3 REST calls → graceful '⚠️' reply, no crash."""
    update, context, _, mock_http = _make_callback_update_and_context("project_detail_3")
    # First call (fetch_project) raises → graceful handling kicks in.
    mock_http.get.side_effect = httpx.ReadTimeout("timed out")

    with patch(
        "telegram_bot.handlers.callback_handler.auth_check",
        new_callable=AsyncMock,
        return_value=(True, _PAIRED_USER_ID),
    ):
        await project_detail_callback(update, context)

    update.callback_query.answer.assert_awaited_once()
    text = update.callback_query.message.reply_text.call_args[0][0]
    assert "⚠️" in text or "couldn't" in text.lower()


# ---------------------------------------------------------------------------
# Tests: task_done
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_done_success():
    """task_done callback marks a task done via PUT /api/tasks/{id} {status: done}."""
    update, context, _, mock_http = _make_callback_update_and_context("task_done_10")
    mock_http.put.return_value = make_http_response({"id": 10, "status": "done"})

    with patch(
        "telegram_bot.handlers.callback_handler.auth_check",
        new_callable=AsyncMock,
        return_value=(True, _PAIRED_USER_ID),
    ):
        await task_done_callback(update, context)

    update.callback_query.answer.assert_awaited_once()
    text = update.callback_query.message.reply_text.call_args[0][0]
    assert "done" in text.lower() or "10" in text

    mock_http.put.assert_awaited_once()
    put_call = mock_http.put.await_args
    assert put_call.args[0].endswith("/api/tasks/10")
    assert put_call.kwargs["json"] == {"status": "done"}
    assert put_call.kwargs["headers"].get("X-Owner-User-Id") == str(_PAIRED_USER_ID)


@pytest.mark.asyncio
async def test_task_done_not_found():
    """task_done callback sends 'not found' when the LE endpoint returns 404.

    A 404 covers both genuinely missing tasks and non-owned ones (ownership is
    enforced server-side), so there is no existence leak.
    """
    update, context, _, mock_http = _make_callback_update_and_context("task_done_999")
    mock_http.put.return_value = make_http_response(None, status=404)

    with patch(
        "telegram_bot.handlers.callback_handler.auth_check",
        new_callable=AsyncMock,
        return_value=(True, _PAIRED_USER_ID),
    ):
        await task_done_callback(update, context)

    text = update.callback_query.message.reply_text.call_args[0][0]
    assert "not found" in text.lower() or "999" in text


@pytest.mark.asyncio
async def test_task_done_service_error_replies_gracefully():
    """R7: a 5xx/timeout from the LE PUT → graceful '⚠️' reply, no hung callback."""
    update, context, _, mock_http = _make_callback_update_and_context("task_done_10")
    mock_http.put.side_effect = httpx.ConnectError("connection refused")

    with patch(
        "telegram_bot.handlers.callback_handler.auth_check",
        new_callable=AsyncMock,
        return_value=(True, _PAIRED_USER_ID),
    ):
        await task_done_callback(update, context)

    update.callback_query.answer.assert_awaited_once()
    text = update.callback_query.message.reply_text.call_args[0][0]
    assert "⚠️" in text or "couldn't" in text.lower()


# ---------------------------------------------------------------------------
# Cross-tenant task-done writes are now blocked server-side.
#
# Post-REST-migration, task_done_callback no longer runs an ownership pre-check
# in the bot — it PUTs to the Learning Engine, which scopes by the forwarded
# X-Owner-User-Id header.  A non-owned task therefore returns 404 (→ "not
# found", no existence leak).  The cross-tenant *denial* guarantee is proven by
# the LE contract test (T8: test_update_task_cross_tenant_returns_404); the bot
# tests below assert the two things the bot is responsible for: forwarding the
# owner identity, and surfacing a 404 as "not found".
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_done_forwards_owner_user_id_for_paired_user():
    """Bot half: the PUT forwards X-Owner-User-Id so the LE can scope."""
    update, context, _, mock_http = _make_paired_callback("task_done_5")
    mock_http.put.return_value = make_http_response({"id": 5, "status": "done"})

    await task_done_callback(update, context)

    mock_http.put.assert_awaited_once()
    put_call = mock_http.put.await_args
    assert put_call.args[0].endswith("/api/tasks/5")
    assert put_call.kwargs["json"] == {"status": "done"}
    assert put_call.kwargs["headers"].get("X-Owner-User-Id") == str(_PAIRED_USER_ID)
    assert put_call.kwargs["headers"].get("X-API-Key") == "test-key"
    text = update.callback_query.message.reply_text.call_args[0][0]
    assert "done" in text.lower() or "5" in text


@pytest.mark.asyncio
async def test_task_done_non_owned_task_returns_not_found_no_leak():
    """Bot half: a non-owned task → LE 404 → 'not found' (no existence leak)."""
    update, context, _, mock_http = _make_paired_callback("task_done_9")
    # The LE scopes by X-Owner-User-Id; another user's task is invisible → 404.
    mock_http.put.return_value = make_http_response(None, status=404)

    await task_done_callback(update, context)

    text = update.callback_query.message.reply_text.call_args[0][0]
    assert "not found" in text.lower() or "9" in text
    # No "marked as done" confirmation leaks for a non-owned task.
    assert "done" not in text.lower() or "not found" in text.lower()


# ---------------------------------------------------------------------------
# Tests: start_review
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_review_callback_delegates_to_review_start():
    """start_review callback delegates to review_start rather than printing a stub message."""
    update, context, _, _ = _make_callback_update_and_context("start_review")
    # Capture the original update.message sentinel so we can assert it is not mutated.
    original_message = update.message

    with patch(
        "telegram_bot.handlers.review_handler.review_start", new_callable=AsyncMock
    ) as mock_review_start:
        await start_review_callback(update, context)

    update.callback_query.answer.assert_awaited_once()
    mock_review_start.assert_awaited_once()
    # Confirm review_start was called with the same update and context as positional args.
    called_args, called_kwargs = mock_review_start.call_args
    assert called_args[0] is update
    assert called_args[1] is context
    # The explicit message kwarg must be the callback query's message — not update.message.
    assert called_kwargs.get("message") is update.callback_query.message
    # update.message must remain unchanged (no mutation of the Update object).
    assert update.message is original_message


@pytest.mark.asyncio
async def test_start_review_callback_does_not_mutate_update_message():
    """start_review_callback must never assign to update.message — Update is immutable per call."""
    update, context, _, _ = _make_callback_update_and_context("start_review")
    sentinel = object()
    update.message = sentinel  # Set a known sentinel value.

    with patch("telegram_bot.handlers.review_handler.review_start", new_callable=AsyncMock):
        await start_review_callback(update, context)

    assert update.message is sentinel, (
        "start_review_callback must not mutate update.message; "
        f"expected sentinel but got {update.message!r}"
    )


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


# ---------------------------------------------------------------------------
# Tests: auth_check before query.answer in all 3 unauthenticated paths
# ---------------------------------------------------------------------------


def _make_unauthed_callback(callback_data: str) -> tuple:
    """Build (update, context, mock_db) for an unauthorised caller (chat_id != 12345)."""
    update, context, mock_db, _ = _make_callback_update_and_context(callback_data, chat_id=99999)
    # auth_check DB path returns None → no paired owner → denied.
    # Also return None for telegram_user_pairings (migration 071) so denial
    # propagates through all three lookup stages.
    mock_db.fetchval.return_value = None
    mock_db.fetchrow.return_value = None
    return update, context, mock_db


@pytest.mark.asyncio
async def test_paper_detail_unauthed_acks_before_returning():
    """paper_detail_callback acks the query even when auth fails.

    auth_check is evaluated BEFORE query.answer; on failure we still call
    query.answer once (H1) so Telegram stops the spinner.
    """
    update, context, _ = _make_unauthed_callback("paper_detail_42")

    await paper_detail_callback(update, context)

    # Must answer exactly once (H1) even on the rejection path
    assert update.callback_query.answer.call_count == 1
    # No paper details are fetched on rejection path
    update.callback_query.message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_project_detail_unauthed_acks_before_returning():
    """project_detail_callback acks the query even when auth fails."""
    update, context, _ = _make_unauthed_callback("project_detail_3")

    await project_detail_callback(update, context)

    assert update.callback_query.answer.call_count == 1
    update.callback_query.message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_task_done_unauthed_acks_before_returning():
    """task_done_callback acks the query even when auth fails."""
    update, context, _ = _make_unauthed_callback("task_done_10")

    await task_done_callback(update, context)

    assert update.callback_query.answer.call_count == 1
    update.callback_query.message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_review_unauthed_acks_before_returning():
    """start_review_callback acks the query even when auth fails."""
    update, context, _ = _make_unauthed_callback("start_review")

    with patch("telegram_bot.handlers.review_handler.review_start", new_callable=AsyncMock):
        await start_review_callback(update, context)

    assert update.callback_query.answer.call_count == 1


# ---------------------------------------------------------------------------
# Cross-user: X-Owner-User-Id forwarded from paired callbacks
# ---------------------------------------------------------------------------

_PAIRED_CHAT_ID = 55555
_PAIRED_USER_ID = 42


def _make_paired_callback(callback_data: str) -> tuple:
    """Build (update, context, mock_db, mock_http) for a paired multi-tenant chat.

    The chat_id does not match the env-var config, so auth_check will query
    telegram_user_pairings.  We set mock_db.fetchrow to return a row with
    user_id=_PAIRED_USER_ID so auth_check grants access as a paired user.
    """
    update, context, mock_db, mock_http = _make_callback_update_and_context(
        callback_data, chat_id=_PAIRED_CHAT_ID
    )
    # auth_check path 1 (env var): no match — chat_id != _TEST_CHAT_ID
    # auth_check path 2 (user_config owner): fetchval returns None
    mock_db.fetchval.return_value = None
    # auth_check path 3 (telegram_user_pairings): return paired row
    mock_db.fetchrow.return_value = {"user_id": _PAIRED_USER_ID}
    return update, context, mock_db, mock_http


@pytest.mark.asyncio
async def test_paper_detail_callback_sends_owner_user_id_for_paired_user():
    """paper_detail_callback includes X-Owner-User-Id for a paired user."""
    update, context, _, mock_http = _make_paired_callback("paper_detail_42")
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {
        "paper": {"title": "Test", "authors": [], "published_date": None, "url": None},
        "summary": None,
    }
    mock_http.get.return_value = mock_resp

    await paper_detail_callback(update, context)

    mock_http.get.assert_awaited_once()
    headers = mock_http.get.await_args[1]["headers"]
    assert headers.get("X-Owner-User-Id") == str(_PAIRED_USER_ID)
    assert headers.get("X-API-Key") == "test-key"


@pytest.mark.asyncio
async def test_paper_action_callback_sends_owner_user_id_for_paired_user():
    """paper_action_callback includes X-Owner-User-Id for a paired user."""
    update, context, _, _ = _make_paired_callback("paper:save:7")
    mock_http = _make_action_mock_http()
    context.application.bot_data["http_client"] = mock_http

    await paper_action_callback(update, context)

    mock_http.request.assert_awaited_once()
    headers = mock_http.request.await_args[1]["headers"]
    assert headers.get("X-Owner-User-Id") == str(_PAIRED_USER_ID)
    assert headers.get("X-API-Key") == "test-key"


@pytest.mark.asyncio
async def test_paper_feedback_callback_sends_owner_user_id_for_paired_user():
    """paper_feedback_callback includes X-Owner-User-Id for a paired user."""
    update, context, _, _ = _make_paired_callback("paper:feedback_pos:7:pulse_thumbs")
    mock_http = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_http.post.return_value = mock_resp
    context.application.bot_data["http_client"] = mock_http

    await paper_feedback_callback(update, context)

    mock_http.post.assert_awaited_once()
    headers = mock_http.post.await_args[1]["headers"]
    assert headers.get("X-Owner-User-Id") == str(_PAIRED_USER_ID)
    assert headers.get("X-API-Key") == "test-key"


@pytest.mark.asyncio
async def test_paper_feedback_callback_uses_paired_user_identity_in_request():
    """TG-02: paper_feedback_callback forwards the authenticated user's identity.

    The assert jarvis_user_id is not None guard (post-auth-check) ensures the
    paired user ID — not None — is threaded into _owner_headers and onward to
    the backend.  Asserts X-Owner-User-Id equals the paired user's id.
    """
    update, context, _, _ = _make_paired_callback("paper:feedback_neg:55:feed_thumbs")
    mock_http = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_http.post.return_value = mock_resp
    context.application.bot_data["http_client"] = mock_http

    await paper_feedback_callback(update, context)

    mock_http.post.assert_awaited_once()
    headers = mock_http.post.await_args[1]["headers"]
    # Must carry the exact paired user ID — not None, not the env chat_id.
    assert headers.get("X-Owner-User-Id") == str(_PAIRED_USER_ID)


# ---------------------------------------------------------------------------
# TG-N2: project_detail user-scoping defense-in-depth
#
# Post-REST-migration the bot no longer issues SQL; per-user scoping is enforced
# by forwarding X-Owner-User-Id on every backend GET (the Learning Engine scopes
# the SQL).  These tests assert the header is forwarded on each of the three
# project-detail GETs (project, tasks, milestones).
# ---------------------------------------------------------------------------


def _project_detail_responses() -> list:
    """Three successful REST responses for project / tasks / milestones."""
    return [
        make_http_response(
            {
                "id": 3,
                "name": "Scoped Project",
                "status": "active",
                "description": None,
                "deadline": None,
            }
        ),
        make_http_response([{"id": 1, "title": "T1", "status": "todo"}]),
        make_http_response([{"id": 1, "name": "M1", "deadline": None, "completed": False}]),
    ]


@pytest.mark.asyncio
async def test_project_detail_with_user_id_scopes_project_fetch() -> None:
    """TG-N2: the project GET forwards X-Owner-User-Id so the LE scopes it."""
    update, context, _, mock_http = _make_paired_callback("project_detail_3")
    mock_http.get.side_effect = _project_detail_responses()

    await project_detail_callback(update, context)

    project_call = mock_http.get.await_args_list[0]
    assert project_call.args[0].endswith("/api/projects/3")
    assert project_call.kwargs["headers"].get("X-Owner-User-Id") == str(_PAIRED_USER_ID)


@pytest.mark.asyncio
async def test_project_detail_with_user_id_scopes_task_fetch() -> None:
    """TG-N2: the tasks GET forwards X-Owner-User-Id so the LE scopes it."""
    update, context, _, mock_http = _make_paired_callback("project_detail_3")
    mock_http.get.side_effect = _project_detail_responses()

    await project_detail_callback(update, context)

    task_call = mock_http.get.await_args_list[1]
    assert task_call.args[0].endswith("/api/projects/3/tasks")
    assert task_call.kwargs["headers"].get("X-Owner-User-Id") == str(_PAIRED_USER_ID)


@pytest.mark.asyncio
async def test_project_detail_with_user_id_scopes_milestone_fetch() -> None:
    """TG-N2: the milestones GET forwards X-Owner-User-Id so the LE scopes it."""
    update, context, _, mock_http = _make_paired_callback("project_detail_3")
    mock_http.get.side_effect = _project_detail_responses()

    await project_detail_callback(update, context)

    milestone_call = mock_http.get.await_args_list[2]
    assert milestone_call.args[0].endswith("/api/projects/3/milestones")
    assert milestone_call.kwargs["headers"].get("X-Owner-User-Id") == str(_PAIRED_USER_ID)


# ---------------------------------------------------------------------------
# TG-003: start_review_callback handles inaccessible message safely
# (migrated from test_tg002_tg003_hardening.py)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_review_callback_handles_inaccessible_message_gracefully():
    """TG-003: start_review_callback sends show_alert and returns early when the
    message is inaccessible (query.message is not a telegram.Message instance).

    The isinstance guard must prevent review_start from being called and must
    answer the query with show_alert=True so Telegram removes the spinner.
    """

    # Build a query where message is NOT spec'd to telegram.Message
    class _FakeInaccessibleMessage:
        pass

    update = MagicMock()
    update.effective_chat = MagicMock()
    update.effective_chat.id = _TEST_CHAT_ID
    update.effective_chat.type = "private"

    query = MagicMock()
    query.data = "start_review"
    query.answer = AsyncMock()
    query.message = _FakeInaccessibleMessage()
    update.callback_query = query

    context = MagicMock()
    context.application = MagicMock()
    context.application.bot_data = {
        "config": make_bot_config(BotConfig, telegram_chat_id=_TEST_CHAT_ID),
        "db_pool": AsyncMock(),
        "http_client": AsyncMock(),
    }

    with patch(
        "telegram_bot.handlers.review_handler.review_start", new_callable=AsyncMock
    ) as mock_review_start:
        await start_review_callback(update, context)

    mock_review_start.assert_not_awaited()
    query.answer.assert_awaited_once_with("This message is no longer accessible", show_alert=True)
