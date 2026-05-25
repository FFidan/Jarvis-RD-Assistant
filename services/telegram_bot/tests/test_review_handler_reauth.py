"""RB-5: Stale-identity cross-tenant regression tests for review handlers.

Asserts that after auth_check resolves a NEW jarvis_user_id, each handler
writes the fresh id back to context.user_data and uses it — not the stale
value that was previously cached there.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jarvis_common.testing import make_bot_config
from telegram_bot.handlers.review_handler import (
    SHOWING_BACK,
    SHOWING_FRONT,
    rate_card,
    review_start,
    show_answer,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_STALE_USER_ID = 10
_FRESH_USER_ID = 20
_TEST_CHAT_ID = 99999


def _make_command_update(chat_id: int = _TEST_CHAT_ID) -> MagicMock:
    update = MagicMock()
    update.effective_chat = MagicMock()
    update.effective_chat.id = chat_id
    update.message = MagicMock()
    update.message.reply_text = AsyncMock()
    update.callback_query = None
    return update


def _make_callback_update(callback_data: str, chat_id: int = _TEST_CHAT_ID) -> MagicMock:
    update = MagicMock()
    update.effective_chat = MagicMock()
    update.effective_chat.id = chat_id
    query = MagicMock()
    query.data = callback_data
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.message = MagicMock()
    update.callback_query = query
    return update


def _make_context(user_data: dict | None = None) -> tuple[MagicMock, AsyncMock]:
    context = MagicMock()
    context.user_data = user_data if user_data is not None else {}
    config = make_bot_config(telegram_chat_id=None)
    mock_http = AsyncMock()
    context.application = MagicMock()
    context.application.bot_data = {
        "config": config,
        "db_pool": AsyncMock(),
        "http_client": mock_http,
    }
    return context, mock_http


def _sample_card(card_id: int = 1) -> dict:
    return {
        "id": card_id,
        "deck_id": 1,
        "paper_id": None,
        "card_type": "concept",
        "front": "Q?",
        "back": "A.",
        "evidence": {},
        "fsrs_state": {},
        "due_at": "2026-03-01T00:00:00Z",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }


# ---------------------------------------------------------------------------
# review_start — reauth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_review_start_refreshes_user_id():
    """review_start writes the fresh auth_check user_id over the stale cached one."""
    update = _make_command_update()
    context, mock_http = _make_context(user_data={"jarvis_user_id": _STALE_USER_ID})

    card = _sample_card()
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = [card]
    mock_http.get = AsyncMock(return_value=mock_resp)

    with patch(
        "telegram_bot.handlers.review_handler.auth_check",
        new=AsyncMock(return_value=(True, _FRESH_USER_ID)),
    ):
        result = await review_start(update, context)

    assert result == SHOWING_FRONT
    assert context.user_data["jarvis_user_id"] == _FRESH_USER_ID, (
        "Expected fresh user_id to overwrite stale cached value"
    )


@pytest.mark.asyncio
async def test_review_start_sets_user_id_when_previously_absent():
    """review_start sets jarvis_user_id even when it was not in user_data before."""
    update = _make_command_update()
    context, mock_http = _make_context(user_data={})  # no prior cached id

    card = _sample_card()
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = [card]
    mock_http.get = AsyncMock(return_value=mock_resp)

    with patch(
        "telegram_bot.handlers.review_handler.auth_check",
        new=AsyncMock(return_value=(True, _FRESH_USER_ID)),
    ):
        result = await review_start(update, context)

    assert result == SHOWING_FRONT
    assert context.user_data["jarvis_user_id"] == _FRESH_USER_ID


@pytest.mark.asyncio
async def test_review_start_does_not_set_user_id_when_unauthorized():
    """review_start must not touch user_data when auth_check denies."""
    update = _make_command_update()
    context, _mock_http = _make_context(user_data={"jarvis_user_id": _STALE_USER_ID})

    with patch(
        "telegram_bot.handlers.review_handler.auth_check",
        new=AsyncMock(return_value=(False, None)),
    ):
        from telegram.ext import ConversationHandler

        result = await review_start(update, context)

    assert result == ConversationHandler.END
    # stale value must be unchanged — we did not write anything
    assert context.user_data["jarvis_user_id"] == _STALE_USER_ID


# ---------------------------------------------------------------------------
# show_answer — reauth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_show_answer_refreshes_user_id():
    """show_answer writes the fresh auth_check user_id over the stale cached one."""
    card = _sample_card()
    update = _make_callback_update("show_answer")
    context, _mock_http = _make_context(
        user_data={"jarvis_user_id": _STALE_USER_ID, "current_card": card}
    )

    with patch(
        "telegram_bot.handlers.review_handler.auth_check",
        new=AsyncMock(return_value=(True, _FRESH_USER_ID)),
    ):
        result = await show_answer(update, context)

    assert result == SHOWING_BACK
    assert context.user_data["jarvis_user_id"] == _FRESH_USER_ID


@pytest.mark.asyncio
async def test_show_answer_does_not_set_user_id_when_unauthorized():
    """show_answer must not touch user_data when auth_check denies."""
    update = _make_callback_update("show_answer")
    context, _mock_http = _make_context(user_data={"jarvis_user_id": _STALE_USER_ID})

    with patch(
        "telegram_bot.handlers.review_handler.auth_check",
        new=AsyncMock(return_value=(False, None)),
    ):
        from telegram.ext import ConversationHandler

        result = await show_answer(update, context)

    assert result == ConversationHandler.END
    assert context.user_data["jarvis_user_id"] == _STALE_USER_ID


# ---------------------------------------------------------------------------
# rate_card — reauth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_card_uses_fresh_user_id_in_http_request():
    """rate_card must pass the fresh user_id (not stale cached) to the HTTP POST."""
    card = _sample_card()
    update = _make_callback_update("rate_3")
    context, mock_http = _make_context(
        user_data={"jarvis_user_id": _STALE_USER_ID, "current_card": card, "cards_reviewed": 0}
    )

    submit_resp = MagicMock()
    submit_resp.raise_for_status = MagicMock()
    submit_resp.json.return_value = {"next_due_at": "2026-03-10T00:00:00Z"}
    # No more cards so session ends cleanly
    next_resp = MagicMock()
    next_resp.raise_for_status = MagicMock()
    next_resp.json.return_value = []
    mock_http.post = AsyncMock(return_value=submit_resp)
    mock_http.get = AsyncMock(return_value=next_resp)

    with patch(
        "telegram_bot.handlers.review_handler.auth_check",
        new=AsyncMock(return_value=(True, _FRESH_USER_ID)),
    ):
        await rate_card(update, context)

    # The X-Jarvis-User-Id header passed to the POST must carry the FRESH id
    post_call_kwargs = mock_http.post.call_args
    headers_sent = post_call_kwargs.kwargs.get("headers") or post_call_kwargs[1].get("headers", {})
    fresh_id_str = str(_FRESH_USER_ID)
    stale_id_str = str(_STALE_USER_ID)
    assert fresh_id_str in str(headers_sent), (
        f"Expected fresh user_id {_FRESH_USER_ID} in POST headers, got: {headers_sent}"
    )
    assert stale_id_str not in str(headers_sent), (
        f"Stale user_id {_STALE_USER_ID} must NOT appear in POST headers"
    )


@pytest.mark.asyncio
async def test_rate_card_refreshes_cached_user_id():
    """rate_card writes the fresh auth_check user_id back to context.user_data."""
    card = _sample_card()
    update = _make_callback_update("rate_3")
    context, mock_http = _make_context(
        user_data={"jarvis_user_id": _STALE_USER_ID, "current_card": card, "cards_reviewed": 0}
    )

    submit_resp = MagicMock()
    submit_resp.raise_for_status = MagicMock()
    submit_resp.json.return_value = {"next_due_at": "2026-03-10T00:00:00Z"}
    next_resp = MagicMock()
    next_resp.raise_for_status = MagicMock()
    next_resp.json.return_value = []
    mock_http.post = AsyncMock(return_value=submit_resp)
    mock_http.get = AsyncMock(return_value=next_resp)

    with patch(
        "telegram_bot.handlers.review_handler.auth_check",
        new=AsyncMock(return_value=(True, _FRESH_USER_ID)),
    ):
        await rate_card(update, context)

    assert context.user_data["jarvis_user_id"] == _FRESH_USER_ID


@pytest.mark.asyncio
async def test_rate_card_does_not_set_user_id_when_unauthorized():
    """rate_card must not touch user_data when auth_check denies."""
    update = _make_callback_update("rate_3")
    context, _mock_http = _make_context(user_data={"jarvis_user_id": _STALE_USER_ID})

    with patch(
        "telegram_bot.handlers.review_handler.auth_check",
        new=AsyncMock(return_value=(False, None)),
    ):
        from telegram.ext import ConversationHandler

        result = await rate_card(update, context)

    assert result == ConversationHandler.END
    assert context.user_data["jarvis_user_id"] == _STALE_USER_ID
