"""Tests for Telegram bot inline-keyboard callback handlers.

Covers: paper_detail, paper_action (9 lifecycle actions), paper_feedback,
project_detail, task_done, start_review.  Legacy bookmark/dismiss/save tests
replaced by the new dispatcher pattern (T1).

WS-AH2 H1 invariant: every test for the dispatcher callbacks asserts
``query.answer.call_count == 1`` on every execution path.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import telegram
from pydantic import SecretStr
from telegram_bot.config import BotConfig
from telegram_bot.handlers import rate_limit as _rate_limit_mod
from telegram_bot.handlers.callback_handler import (
    paper_action_callback,
    paper_detail_callback,
    paper_feedback_callback,
    project_detail_callback,
    start_review_callback,
    task_done_callback,
)

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


def _make_config() -> BotConfig:
    return BotConfig(
        telegram_token="test-token",
        telegram_chat_id=_TEST_CHAT_ID,
        database_url="postgres://test",
        paper_ingestion_url="http://paper:8000",
        learning_engine_url="http://learn:8001",
        jarvis_api_key=SecretStr("test-key"),
    )


def _make_callback_update_and_context(callback_data: str, chat_id: int = _TEST_CHAT_ID):
    """Build (Update, Context, mock_db, mock_http) tuple for callback tests."""
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
    """H1 regression guard: an unauthorised callback still answers the query.

    Without this, the Telegram client spins indefinitely on auth-rejected
    callbacks (Wave-3 review SB-2).
    """
    # Use a chat_id that does NOT match _make_config().telegram_chat_id so
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
    """H1 regression guard for paper_feedback_callback (Wave-3 review SB-2)."""
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
    update, context, *_ = _make_callback_update_and_context("task_done_10")

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
    update, context, *_ = _make_callback_update_and_context("task_done_999")

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
    # Capture the original update.message sentinel so we can assert it is not mutated.
    original_message = update.message

    with patch(
        "telegram_bot.handlers.callback_handler.review_start", new_callable=AsyncMock
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

    with patch("telegram_bot.handlers.callback_handler.review_start", new_callable=AsyncMock):
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
# Tests: W4-4 — auth_check before query.answer in all 3 unauthenticated paths
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
    """W4-4: paper_detail_callback acks the query even when auth fails.

    auth_check is now evaluated BEFORE query.answer; on failure we still call
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
    """W4-4: project_detail_callback acks the query even when auth fails."""
    update, context, _ = _make_unauthed_callback("project_detail_3")

    await project_detail_callback(update, context)

    assert update.callback_query.answer.call_count == 1
    update.callback_query.message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_task_done_unauthed_acks_before_returning():
    """W4-4: task_done_callback acks the query even when auth fails."""
    update, context, _ = _make_unauthed_callback("task_done_10")

    await task_done_callback(update, context)

    assert update.callback_query.answer.call_count == 1
    update.callback_query.message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_review_unauthed_acks_before_returning():
    """W4-4: start_review_callback acks the query even when auth fails."""
    update, context, _ = _make_unauthed_callback("start_review")

    with patch("telegram_bot.handlers.callback_handler.review_start", new_callable=AsyncMock):
        await start_review_callback(update, context)

    assert update.callback_query.answer.call_count == 1


# ---------------------------------------------------------------------------
# WS-CROSS-USER: X-Owner-User-Id forwarded from paired callbacks
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
    """WS-CROSS-USER: paper_detail_callback includes X-Owner-User-Id for a paired user."""
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
    """WS-CROSS-USER: paper_action_callback includes X-Owner-User-Id for a paired user."""
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
    """WS-CROSS-USER: paper_feedback_callback includes X-Owner-User-Id for a paired user."""
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


# ---------------------------------------------------------------------------
# TG-N2: project_detail user-scoping defense-in-depth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_project_detail_with_user_id_scopes_project_fetch() -> None:
    """TG-N2: when user_id is present, project fetch uses IS NOT DISTINCT FROM $2."""
    update, context, mock_db, _ = _make_paired_callback("project_detail_3")
    # auth_check calls fetchrow for the pairing lookup (first call);
    # project_detail_callback then calls fetchrow for the project (second call).
    project_row = {
        "id": 3,
        "name": "Scoped Project",
        "status": "active",
        "description": None,
        "deadline": None,
    }
    mock_db.fetchrow.side_effect = [
        {"user_id": _PAIRED_USER_ID},  # auth_check pairing lookup
        project_row,  # project fetch
    ]
    mock_db.fetch.return_value = []

    await project_detail_callback(update, context)

    # The project fetchrow is the SECOND call
    fetchrow_call = mock_db.fetchrow.call_args_list[1]
    sql = fetchrow_call.args[0]
    args = fetchrow_call.args[1:]
    assert "IS NOT DISTINCT FROM $2" in sql, (
        f"project fetch must use IS NOT DISTINCT FROM $2; SQL={sql!r}"
    )
    assert args[0] == 3, f"$1 must be project_id=3; got {args[0]!r}"
    assert args[1] == _PAIRED_USER_ID, f"$2 must be user_id={_PAIRED_USER_ID}; got {args[1]!r}"


@pytest.mark.asyncio
async def test_project_detail_with_user_id_scopes_task_fetch() -> None:
    """TG-N2: tasks query must include user_id predicate after project gate."""
    update, context, mock_db, _ = _make_paired_callback("project_detail_3")
    mock_db.fetchrow.side_effect = [
        {"user_id": _PAIRED_USER_ID},  # auth_check pairing lookup
        {"id": 3, "name": "P", "status": "active", "description": None, "deadline": None},
    ]
    # Two fetch calls: tasks then milestones
    mock_db.fetch.side_effect = [
        [{"id": 1, "title": "T1", "status": "todo"}],
        [],
    ]

    await project_detail_callback(update, context)

    task_fetch_call = mock_db.fetch.call_args_list[0]
    task_sql = task_fetch_call.args[0]
    task_args = task_fetch_call.args[1:]
    assert "IS NOT DISTINCT FROM $2" in task_sql, (
        f"tasks query must scope by user_id; SQL={task_sql!r}"
    )
    assert task_args[0] == 3, f"$1 must be project_id=3; got {task_args[0]!r}"
    assert task_args[1] == _PAIRED_USER_ID, f"$2 must be user_id; got {task_args[1]!r}"


@pytest.mark.asyncio
async def test_project_detail_with_user_id_scopes_milestone_fetch() -> None:
    """TG-N2: milestones query must include user_id predicate after project gate."""
    update, context, mock_db, _ = _make_paired_callback("project_detail_3")
    mock_db.fetchrow.side_effect = [
        {"user_id": _PAIRED_USER_ID},  # auth_check pairing lookup
        {"id": 3, "name": "P", "status": "active", "description": None, "deadline": None},
    ]
    mock_db.fetch.side_effect = [
        [],
        [{"id": 1, "name": "M1", "deadline": None, "completed": False}],
    ]

    await project_detail_callback(update, context)

    milestone_fetch_call = mock_db.fetch.call_args_list[1]
    milestone_sql = milestone_fetch_call.args[0]
    milestone_args = milestone_fetch_call.args[1:]
    assert "IS NOT DISTINCT FROM $2" in milestone_sql, (
        f"milestones query must scope by user_id; SQL={milestone_sql!r}"
    )
    assert milestone_args[1] == _PAIRED_USER_ID, f"$2 must be user_id; got {milestone_args[1]!r}"


@pytest.mark.asyncio
async def test_project_detail_none_user_id_scoped_to_null_rows_only() -> None:
    """TG-N2: legacy single-tenant path scopes project fetch to user_id IS NULL.

    Previously, the None branch used an unscoped SELECT that could return ANY
    project by id.  After the fix, IS NOT DISTINCT FROM NULL restricts the
    result to rows where user_id IS NULL — closing the unscoped catch-all.
    """
    # Single-tenant legacy path: auth_check returns (True, None)
    update, context, mock_db, _ = _make_callback_update_and_context(
        "project_detail_7", chat_id=_TEST_CHAT_ID
    )
    # The env-var path returns jarvis_user_id=None for the legacy owner
    mock_db.fetchrow.return_value = None  # project not found under user_id=NULL

    await project_detail_callback(update, context)

    fetchrow_call = mock_db.fetchrow.call_args
    sql = fetchrow_call.args[0]
    bound_user_id = fetchrow_call.args[2]  # third positional arg = $2
    assert "IS NOT DISTINCT FROM $2" in sql, (
        f"None path must use IS NOT DISTINCT FROM $2 (not unscoped); SQL={sql!r}"
    )
    assert bound_user_id is None, (
        f"$2 must be None (NULL) for legacy single-tenant path; got {bound_user_id!r}"
    )
