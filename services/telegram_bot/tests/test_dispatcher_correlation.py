"""Tests for correlation-id middleware and event emission in the auth_required decorator.

Covers:
- test_dispatcher_sets_correlation_id_per_command: each invocation gets a fresh UUID
  in the correlation_id ContextVar; the var is reset after the call.
- test_dispatcher_emits_auth_event_on_first_message_per_chat: first call emits
  category='auth'; second call from the same chat does not.
- test_dispatcher_emits_config_event_when_setting_changes: successful pairing emits
  category='config'.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jarvis_common.logging_config import correlation_id_var
from jarvis_common.testing import make_bot_config, make_telegram_update
from telegram_bot.config import BotConfig
from telegram_bot.handlers import rate_limit as _rate_limit_mod
from telegram_bot.handlers.commands._auth import _SEEN_CHATS, _maybe_emit_auth_event

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_state():
    """Clear shared module-level state before every test."""
    _rate_limit_mod._timestamps.clear()
    _SEEN_CHATS.clear()
    yield
    _rate_limit_mod._timestamps.clear()
    _SEEN_CHATS.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TEST_CHAT_ID = 99001


def _make_pool():
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=None)
    pool.execute = AsyncMock(return_value="OK")
    return pool


def _make_context(pool, config):
    context = MagicMock()
    context.application = MagicMock()
    context.application.bot_data = {
        "config": config,
        "db_pool": pool,
        "http_client": AsyncMock(),
    }
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
    """_maybe_emit_auth_event emits exactly once per chat_id.

    First call: log_event is awaited.
    Second call from same chat_id: log_event is NOT called again.
    """
    mock_pool = _make_pool()
    mock_log_event = AsyncMock()

    with patch("telegram_bot.handlers.commands._auth.log_event", mock_log_event):
        await _maybe_emit_auth_event(_TEST_CHAT_ID, mock_pool)
        await _maybe_emit_auth_event(_TEST_CHAT_ID, mock_pool)

    assert mock_log_event.await_count == 1, (
        f"log_event should be called exactly once for a chat_id, got {mock_log_event.await_count}"
    )
    call_kwargs = mock_log_event.await_args.kwargs
    assert call_kwargs["category"] == "auth"
    assert call_kwargs["message"] == "chat_active"
    assert call_kwargs["context"]["chat_id"] == _TEST_CHAT_ID


@pytest.mark.asyncio
async def test_dispatcher_auth_event_not_emitted_for_second_distinct_chat():
    """Each distinct chat_id triggers its own auth event exactly once."""
    mock_pool = _make_pool()
    mock_log_event = AsyncMock()

    chat_a = 11111
    chat_b = 22222

    with patch("telegram_bot.handlers.commands._auth.log_event", mock_log_event):
        await _maybe_emit_auth_event(chat_a, mock_pool)
        await _maybe_emit_auth_event(chat_b, mock_pool)
        # Second call for each — must NOT fire again
        await _maybe_emit_auth_event(chat_a, mock_pool)
        await _maybe_emit_auth_event(chat_b, mock_pool)

    assert mock_log_event.await_count == 2


@pytest.mark.asyncio
async def test_dispatcher_emits_config_event_when_setting_changes():
    """Successful /start PAIR_<code> flow emits a category='config' event.

    The event must be emitted after the user_config upsert succeeds but before
    the confirmation reply.
    """
    from telegram_bot.handlers.commands.system_commands import start_command

    future = datetime.now(UTC) + timedelta(minutes=5)

    # Build a conn that succeeds through the full pairing flow:
    # fetchval → None (no existing owner), fetchrow → valid expiry, execute → OK
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=None)
    conn.fetchrow = AsyncMock(return_value={"expires_at": future})
    conn.execute = AsyncMock(return_value="EXECUTE 1")

    class _TxnCM:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

    class _AcquireCM:
        def __init__(self, c):
            self._c = c

        async def __aenter__(self):
            return self._c

        async def __aexit__(self, *_):
            return None

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AcquireCM(conn))
    pool.fetchval = AsyncMock(return_value=None)
    conn.transaction = MagicMock(return_value=_TxnCM())

    update = MagicMock()
    update.effective_chat = MagicMock()
    update.effective_chat.id = 42
    update.message = MagicMock()
    update.message.text = "/start PAIR_VALIDCODE"
    update.message.reply_text = AsyncMock()

    context = _make_context(pool, make_bot_config(BotConfig, telegram_chat_id=None))

    mock_log_event = AsyncMock()

    # Patch log_event in both modules that import it
    with (
        patch("telegram_bot.handlers.commands.system_commands.log_event", mock_log_event),
        patch("telegram_bot.handlers.commands._auth.log_event", AsyncMock()),
    ):
        await start_command(update, context)

    config_calls = [
        c for c in mock_log_event.await_args_list if c.kwargs.get("category") == "config"
    ]
    assert len(config_calls) == 1, (
        f"Expected exactly one config event, got {len(config_calls)}: {config_calls}"
    )
    kw = config_calls[0].kwargs
    assert kw["message"] == "setting_changed"
    assert kw["context"]["command"] == "start_pairing"
    assert kw["context"]["chat_id"] == 42
