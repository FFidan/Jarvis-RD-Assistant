"""Security tests for the /start PAIR_<code> pairing flow.

Covers:
- H3: existing-owner check — second pairing attempt is rejected without overwriting
- H4: exception logging uses code_hash, not raw code
- Rate-limit: 6th attempt within 60 s from the same chat is rejected
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import SecretStr
from telegram_bot.config import BotConfig
from telegram_bot.handlers.commands.system_commands import _handle_pairing  # noqa: E402
from telegram_bot.handlers.rate_limit import _timestamps  # noqa: E402

_OWNER_CHAT_ID = 777


def _make_config(telegram_chat_id: int | None = None) -> BotConfig:
    return BotConfig(
        telegram_token="test-token",
        telegram_chat_id=telegram_chat_id,  # type: ignore[arg-type]
        database_url="postgres://test",
        paper_ingestion_url="http://paper:8000",
        learning_engine_url="http://learn:8001",
        jarvis_api_key=SecretStr("test-key"),
    )


class _FakeAcquireCM:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *_):
        return None


class _FakeTxnCM:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None


def _make_conn(
    *,
    fetchval_return=None,  # existing owner value (None = no owner set)
    fetchrow_return=None,  # telegram_pairing row
):
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=fetchval_return)
    conn.fetchrow = AsyncMock(return_value=fetchrow_return)
    conn.execute = AsyncMock(return_value="EXECUTE 1")
    conn.transaction = MagicMock(return_value=_FakeTxnCM())
    return conn


def _make_pool(conn):
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_FakeAcquireCM(conn))
    return pool


def _make_update(chat_id: int = _OWNER_CHAT_ID):
    update = MagicMock()
    update.effective_chat = MagicMock()
    update.effective_chat.id = chat_id
    update.message = MagicMock()
    update.message.reply_text = AsyncMock()
    return update


def _make_context(pool, config):
    context = MagicMock()
    context.application = MagicMock()
    context.application.bot_data = {
        "config": config,
        "db_pool": pool,
        "http_client": AsyncMock(),
    }
    return context


@pytest.fixture(autouse=True)
def clear_rate_limit_state():
    """Reset rate-limit timestamps before every test to avoid cross-test pollution."""
    _timestamps.clear()
    yield
    _timestamps.clear()


# ---------------------------------------------------------------------------
# H3: existing-owner check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_already_paired_rejected():
    """If an owner is already stored, pairing is refused and DB is NOT overwritten."""
    # Simulate an existing owner: DB returns a non-null value for owner_chat_id
    existing_owner_json = '"999"'
    conn = _make_conn(fetchval_return=existing_owner_json)
    pool = _make_pool(conn)
    update = _make_update(chat_id=42)
    context = _make_context(pool, _make_config())

    await _handle_pairing(update, context, "VALID_CODE")

    # The pairing code lookup (fetchrow) must NOT have been called
    conn.fetchrow.assert_not_awaited()
    # No writes must have happened
    conn.execute.assert_not_awaited()
    # User gets the "already paired" message
    update.message.reply_text.assert_awaited_once()
    reply_text = update.message.reply_text.call_args[0][0]
    assert "already paired" in reply_text


@pytest.mark.asyncio
async def test_already_paired_integer_owner_rejected():
    """Owner stored as bare integer string is also treated as already-paired."""
    conn = _make_conn(fetchval_return="777")
    pool = _make_pool(conn)
    update = _make_update(chat_id=42)
    context = _make_context(pool, _make_config())

    await _handle_pairing(update, context, "SOME_CODE")

    conn.execute.assert_not_awaited()
    reply_text = update.message.reply_text.call_args[0][0]
    assert "already paired" in reply_text


@pytest.mark.asyncio
async def test_no_existing_owner_allows_pairing():
    """When no owner is set, valid pairing code is accepted and owner is written."""
    future = datetime.now(UTC) + timedelta(minutes=5)
    conn = _make_conn(
        fetchval_return=None,  # no existing owner
        fetchrow_return={"expires_at": future},
    )
    pool = _make_pool(conn)
    update = _make_update(chat_id=42)
    context = _make_context(pool, _make_config())

    await _handle_pairing(update, context, "GOODCODE")

    # Owner write + code deletion
    assert conn.execute.await_count == 2
    update.message.reply_text.assert_awaited_once()
    reply_text = update.message.reply_text.call_args[0][0]
    assert "Paired" in reply_text


# ---------------------------------------------------------------------------
# H4: raw code must NOT appear in exception logs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pairing_logs_hash_not_raw_code(caplog):
    """On exception inside _handle_pairing, raw code must NOT appear in log output."""
    import hashlib
    import logging

    # Force an exception by making fetchval raise
    conn = _make_conn()
    conn.fetchval = AsyncMock(side_effect=RuntimeError("db exploded"))
    pool = _make_pool(conn)
    update = _make_update(chat_id=42)
    context = _make_context(pool, _make_config())

    raw_code = "SUPERSECRETCODE123"

    with caplog.at_level(logging.ERROR):
        await _handle_pairing(update, context, raw_code)

    # The raw code must not appear anywhere in the captured log
    for record in caplog.records:
        assert raw_code not in record.getMessage(), (
            f"Raw pairing code leaked in log: {record.getMessage()!r}"
        )

    # A log entry for pairing failure must exist
    failure_messages = [
        r.getMessage() for r in caplog.records if "pairing_failed" in r.getMessage()
    ]
    assert failure_messages, "Expected at least one 'pairing_failed' log entry"

    # The hash (8-char hex) should appear instead of the raw code
    expected_hash = hashlib.sha256(raw_code.encode()).hexdigest()[:8]
    assert any(expected_hash in msg for msg in failure_messages), (
        f"Expected code_hash {expected_hash!r} in log, got: {failure_messages}"
    )


# ---------------------------------------------------------------------------
# Rate limit: PAIR_ branch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pairing_rate_limit_triggers():
    """6th pairing attempt within 60 s from the same chat_id must be rejected."""
    # Use an always-invalid code so each attempt quickly returns "Invalid or expired"
    # and doesn't try to actually write. conn.fetchrow returns None (unknown code).
    conn = _make_conn(fetchval_return=None, fetchrow_return=None)
    pool = _make_pool(conn)
    update = _make_update(chat_id=42)
    context = _make_context(pool, _make_config())

    # First 5 calls should reach DB (get "Invalid or expired" reply)
    for i in range(5):
        update.message.reply_text.reset_mock()
        await _handle_pairing(update, context, f"BAD_CODE_{i}")
        reply_text = update.message.reply_text.call_args[0][0]
        assert "Invalid or expired" in reply_text, f"Attempt {i + 1} should hit DB"

    # 6th call should be rate-limited before hitting DB
    fetchrow_count_before = conn.fetchrow.await_count
    update.message.reply_text.reset_mock()
    await _handle_pairing(update, context, "BAD_CODE_5")

    # fetchrow must NOT have been called for the 6th attempt
    assert conn.fetchrow.await_count == fetchrow_count_before, (
        "6th attempt should have been rate-limited before reaching DB"
    )
    reply_text = update.message.reply_text.call_args[0][0]
    assert (
        "Rate limit exceeded" in reply_text
        or "Too many pairing" in reply_text
        or "wait" in reply_text.lower()
    ), f"Expected rate-limit message, got: {reply_text!r}"


@pytest.mark.asyncio
async def test_pairing_rate_limit_different_chats_independent():
    """Rate limit is per-chat — two different chat IDs each get their own counter."""
    conn = _make_conn(fetchval_return=None, fetchrow_return=None)
    pool = _make_pool(conn)
    context = _make_context(pool, _make_config())

    # Exhaust limit for chat_id=10
    update_a = _make_update(chat_id=10)
    for _ in range(5):
        await _handle_pairing(update_a, context, "CODE_A")

    # chat_id=20 should still be under the limit
    update_b = _make_update(chat_id=20)
    update_b.message.reply_text.reset_mock()
    await _handle_pairing(update_b, context, "CODE_B")

    reply_text = update_b.message.reply_text.call_args[0][0]
    assert "Too many pairing attempts" not in reply_text, (
        "Different chat_id should not be rate-limited"
    )
