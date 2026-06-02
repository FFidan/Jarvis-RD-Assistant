"""Tests for correlation-id middleware and event emission in the auth_required decorator.

Covers:
- test_dispatcher_sets_correlation_id_per_command: each invocation gets a fresh UUID
  in the correlation_id ContextVar; the var is reset after the call.
- test_dispatcher_emits_auth_event_on_first_message_per_chat: first call emits
  category='auth'; second call from the same chat does not (per-session flag via
  context.user_data["_auth_seen"]).
- test_start_*: /start authenticates via the telegram_user_pairings lookup —
  paired chats get the welcome, unpaired chats get the /pair guidance.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jarvis_common.logging_config import correlation_id_var
from jarvis_common.testing import make_bot_config, make_telegram_update
from telegram_bot.config import BotConfig
from telegram_bot.handlers import rate_limit as _rate_limit_mod

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_state():
    """Clear shared module-level state before every test."""
    _rate_limit_mod._timestamps.clear()
    yield
    _rate_limit_mod._timestamps.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TEST_CHAT_ID = 99001


def _make_pool():
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=None)
    pool.execute = AsyncMock(return_value="OK")
    return pool


def _make_context(pool, config, user_data=None):
    context = MagicMock()
    context.application = MagicMock()
    context.application.bot_data = {
        "config": config,
        "db_pool": pool,
        "http_client": AsyncMock(),
    }
    context.user_data = user_data if user_data is not None else {}
    return context


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatcher_sets_correlation_id_per_command():
    """auth_required sets a fresh UUID in correlation_id_var per invocation.

    Verifies:
    - The var is set (non-None) *inside* the wrapped handler.
    - Two successive calls each get a *different* UUID.
    - After the call, the var is reset to its pre-call value (None).
    """
    captured: list[uuid.UUID | None] = []

    from telegram_bot.handlers.commands._auth import auth_required

    @auth_required
    async def _handler(update, context):
        captured.append(correlation_id_var.get())

    pool = _make_pool()
    update = make_telegram_update(chat_id=_TEST_CHAT_ID)
    context = _make_context(pool, make_bot_config(BotConfig, telegram_chat_id=_TEST_CHAT_ID))
    context.user_data = {"jarvis_user_id": _TEST_CHAT_ID}

    with (
        patch("telegram_bot.handlers.commands._auth.log_event", new_callable=AsyncMock),
        patch(
            "telegram_bot.handlers.commands._auth.auth_check",
            new_callable=AsyncMock,
            return_value=(True, _TEST_CHAT_ID),
        ),
    ):
        await _handler(update, context)
        await _handler(update, context)

    assert len(captured) == 2, "handler must have been called twice"
    assert captured[0] is not None, "correlation_id must be set inside handler"
    assert captured[1] is not None
    assert captured[0] != captured[1], "each call must receive a distinct correlation UUID"

    # After both calls the ContextVar is back to its sentinel (None)
    assert correlation_id_var.get() is None


@pytest.mark.asyncio
async def test_dispatcher_emits_auth_event_on_first_message_per_chat():
    """auth_required emits a category='auth' event exactly once per chat session.

    The first call sets ``context.user_data["_auth_seen"] = True`` and fires
    log_event.  The second call from the same context (same user_data dict) does
    NOT fire log_event again.
    """
    from telegram_bot.handlers.commands._auth import auth_required

    pool = _make_pool()
    config = make_bot_config(BotConfig, telegram_chat_id=_TEST_CHAT_ID)
    update = make_telegram_update(chat_id=_TEST_CHAT_ID)
    # Shared user_data dict — simulates the same PTB per-chat session store.
    shared_user_data: dict = {}
    context = _make_context(pool, config, user_data=shared_user_data)

    mock_log_event = AsyncMock()

    @auth_required
    async def _handler(update, context):
        pass

    with (
        patch("telegram_bot.handlers.commands._auth.log_event", mock_log_event),
        patch(
            "telegram_bot.handlers.commands._auth.auth_check",
            new_callable=AsyncMock,
            return_value=(True, _TEST_CHAT_ID),
        ),
    ):
        await _handler(update, context)
        await _handler(update, context)

    # log_event must fire exactly once (first call sets the flag; second skips)
    auth_calls = [c for c in mock_log_event.await_args_list if c.kwargs.get("category") == "auth"]
    assert len(auth_calls) == 1, (
        f"log_event(category='auth') should be called exactly once, got {len(auth_calls)}"
    )
    call_kwargs = auth_calls[0].kwargs
    assert call_kwargs["message"] == "chat_active"
    assert call_kwargs["context"]["chat_id"] == _TEST_CHAT_ID


@pytest.mark.asyncio
async def test_dispatcher_auth_event_not_emitted_for_second_distinct_chat():
    """Each distinct chat (distinct user_data dict) triggers its own auth event exactly once."""
    from telegram_bot.handlers.commands._auth import auth_required

    pool = _make_pool()
    config = make_bot_config(BotConfig, telegram_chat_id=None)
    mock_log_event = AsyncMock()

    @auth_required
    async def _handler(update, context):
        pass

    chat_a_id = 11111
    chat_b_id = 22222

    update_a = make_telegram_update(chat_id=chat_a_id)
    update_b = make_telegram_update(chat_id=chat_b_id)
    # Different user_data dicts → different per-chat sessions
    context_a = _make_context(pool, config, user_data={})
    context_b = _make_context(pool, config, user_data={})

    with (
        patch("telegram_bot.handlers.commands._auth.log_event", mock_log_event),
        patch(
            "telegram_bot.handlers.commands._auth.auth_check",
            new_callable=AsyncMock,
            return_value=(True, 999),
        ),
    ):
        await _handler(update_a, context_a)
        await _handler(update_b, context_b)
        # Second calls — must NOT fire again for either chat
        await _handler(update_a, context_a)
        await _handler(update_b, context_b)

    auth_calls = [c for c in mock_log_event.await_args_list if c.kwargs.get("category") == "auth"]
    assert len(auth_calls) == 2, (
        f"Expected exactly 2 auth events (one per chat), got {len(auth_calls)}"
    )


@pytest.mark.asyncio
async def test_start_paired_chat_sends_welcome_via_pairing_lookup():
    """/start authenticates via the telegram_user_pairings lookup (no PAIR_
    deep-link, no config event). A paired chat receives the welcome message."""
    from telegram_bot.handlers.commands.system_commands import start_command

    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value={"user_id": 1})  # paired

    update = MagicMock()
    update.effective_chat = MagicMock()
    update.effective_chat.id = 42
    update.message = MagicMock()
    update.message.text = "/start"
    update.message.reply_text = AsyncMock()

    context = _make_context(pool, make_bot_config(BotConfig, telegram_chat_id=None))

    await start_command(update, context)

    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert "Welcome" in text


@pytest.mark.asyncio
async def test_start_unpaired_chat_shows_pair_guidance():
    """/start from an unpaired chat replies with /pair guidance, not welcome."""
    from telegram_bot.handlers.commands.system_commands import start_command

    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value=None)  # unpaired

    update = MagicMock()
    update.effective_chat = MagicMock()
    update.effective_chat.id = 42
    update.message = MagicMock()
    update.message.text = "/start"
    update.message.reply_text = AsyncMock()

    context = _make_context(pool, make_bot_config(BotConfig, telegram_chat_id=None))

    await start_command(update, context)

    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.call_args[0][0]
    assert "/pair" in text
    assert "Welcome" not in text
