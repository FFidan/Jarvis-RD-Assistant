"""Contract tests for telegram_bot pairing handlers.

Uses a real Postgres connection (contract_conn) via the Wave-4 txn-rollback
fixture.  The Telegram Bot API boundary (reply_text, bot.send_message) stays
mocked — that is an external PTB boundary, per the D8 carve-out.

Run with:
    JARVIS_RUN_LIVE_PG=1 uv run pytest --override-ini="addopts=--import-mode=importlib" \
        -m contract services/telegram_bot/tests/contract/
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = [pytest.mark.contract, pytest.mark.asyncio(loop_scope="session")]


# ---------------------------------------------------------------------------
# Pool adapter
# ---------------------------------------------------------------------------
# whoami_command calls db_pool.fetchrow() directly on the pool object (no
# acquire()).  pair_command / unpair_command call pool.acquire() and then
# call methods on the returned connection.
#
# TgContractPool wraps a single real asyncpg connection and satisfies BOTH
# call patterns within the same outer transaction (so all writes are rolled
# back by the contract_conn fixture at teardown).


class _SharedAcquireCM:
    """Async CM returned by TgContractPool.acquire()."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def __aenter__(self) -> Any:
        return self._conn

    async def __aexit__(self, *_: Any) -> None:
        return None


class TgContractPool:
    """Pool-shaped adapter that wraps a single real asyncpg connection.

    Supports both:
      - pool.acquire() → async CM yielding the same conn (for pair/unpair)
      - pool.fetchrow() / pool.fetchval() / pool.fetch() → delegates to conn
        (for whoami_command which calls pool methods directly)
    """

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def acquire(self) -> _SharedAcquireCM:
        return _SharedAcquireCM(self._conn)

    async def fetchrow(self, query: str, *args: Any) -> Any:
        return await self._conn.fetchrow(query, *args)

    async def fetchval(self, query: str, *args: Any) -> Any:
        return await self._conn.fetchval(query, *args)

    async def fetch(self, query: str, *args: Any) -> Any:
        return await self._conn.fetch(query, *args)

    async def execute(self, query: str, *args: Any) -> Any:
        return await self._conn.execute(query, *args)

    async def close(self) -> None:
        return None


# ---------------------------------------------------------------------------
# DB seeding helpers
# ---------------------------------------------------------------------------


async def _seed_user(conn: Any, email: str) -> int:
    """Insert a minimal users row; return user_id."""
    user_id: int = await conn.fetchval(
        "INSERT INTO users (email, role) VALUES ($1, 'user') RETURNING id",
        email,
    )
    return user_id


async def _seed_pairing_token(
    conn: Any,
    user_id: int,
    token: str = "test-token-abc",
    *,
    expires_in: timedelta = timedelta(minutes=15),
    consumed_at: datetime | None = None,
) -> str:
    """Insert a telegram_pairing_tokens row; return the token."""
    await conn.execute(
        """INSERT INTO telegram_pairing_tokens
               (token, user_id, expires_at, consumed_at)
           VALUES ($1, $2, $3, $4)""",
        token,
        user_id,
        datetime.now(UTC) + expires_in,
        consumed_at,
    )
    return token


def _make_context(pool: Any, config: Any = None, *, args: list[str] | None = None) -> MagicMock:
    """Build a minimal PTB context mock wired to the given pool."""
    from jarvis_common.testing import make_bot_config

    ctx = MagicMock()
    ctx.args = args or []
    ctx.bot = MagicMock()
    ctx.bot.send_message = AsyncMock()
    ctx.application = MagicMock()
    ctx.application.bot_data = {
        "config": config or make_bot_config(telegram_chat_id=None),
        "db_pool": pool,
        "http_client": AsyncMock(),
    }
    return ctx


# ---------------------------------------------------------------------------
# Contract: pair_command persists to telegram_user_pairings
# ---------------------------------------------------------------------------


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_pair_command_persists_pairing(contract_conn):
    """pair_command: DB write to telegram_user_pairings is real.

    PTB boundary (reply_text) stays mocked.
    """
    from jarvis_common.testing import make_telegram_update
    from telegram_bot.handlers.commands.pairing_commands import pair_command
    from telegram_bot.handlers.rate_limit import _timestamps

    _timestamps.clear()

    user_id = await _seed_user(contract_conn, "tg-contract-pair@test.local")
    token = await _seed_pairing_token(contract_conn, user_id, "contract-pair-token-001")

    pool = TgContractPool(contract_conn)
    update = make_telegram_update(chat_id=8801, username="contractuser")
    context = _make_context(pool, args=[token])

    await pair_command(update, context)

    # Real DB assertion: pairing row must exist
    row = await contract_conn.fetchrow(
        "SELECT user_id, chat_id, telegram_username FROM telegram_user_pairings WHERE user_id = $1",
        user_id,
    )
    assert row is not None, "telegram_user_pairings row must be created by pair_command"
    assert row["chat_id"] == 8801
    assert row["telegram_username"] == "contractuser"

    # Token must be marked consumed
    token_row = await contract_conn.fetchrow(
        "SELECT consumed_at FROM telegram_pairing_tokens WHERE token = $1",
        token,
    )
    assert token_row is not None
    assert token_row["consumed_at"] is not None, "Token must be marked consumed after pairing"

    # PTB assertion: success reply was sent (the mock interaction)
    update.message.reply_text.assert_awaited_once()
    reply_text: str = update.message.reply_text.call_args[0][0]
    assert "Paired" in reply_text, f"Expected 'Paired' in reply; got: {reply_text!r}"


# ---------------------------------------------------------------------------
# Contract: whoami_command reads real pairing row
# ---------------------------------------------------------------------------


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_whoami_command_reads_real_pairing(contract_conn):
    """whoami_command: reads telegram_user_pairings from real DB.

    PTB boundary (reply_text) stays mocked.
    """
    from jarvis_common.testing import make_bot_config, make_telegram_update
    from telegram_bot.handlers.commands.pairing_commands import whoami_command
    from telegram_bot.handlers.rate_limit import _timestamps

    _timestamps.clear()

    user_id = await _seed_user(contract_conn, "tg-contract-whoami@test.local")
    # Insert a pairing row directly (bypass pair_command for isolation)
    await contract_conn.execute(
        """INSERT INTO telegram_user_pairings (user_id, chat_id, telegram_username, paired_at)
           VALUES ($1, $2, $3, NOW())""",
        user_id,
        9901,
        "whoamiuser",
    )

    pool = TgContractPool(contract_conn)
    update = make_telegram_update(chat_id=9901)
    # telegram_chat_id=None so the legacy-owner branch is not taken
    config = make_bot_config(telegram_chat_id=None)
    context = _make_context(pool, config=config)

    await whoami_command(update, context)

    update.message.reply_text.assert_awaited_once()
    reply_text: str = update.message.reply_text.call_args[0][0]
    assert "Paired" in reply_text, f"Expected 'Paired' in /whoami reply; got: {reply_text!r}"
    # Date of pairing must appear (real paired_at from DB)
    import re

    assert re.search(r"\d{4}-\d{2}-\d{2}", reply_text), (
        f"Expected paired-at date in /whoami reply; got: {reply_text!r}"
    )
    # DOM-D-07: raw DB PK must not leak as a standalone identifier.
    # Use a regex word-boundary check so incidental digit overlaps with dates
    # (e.g. user_id=2 inside "2026-05-21") do not produce false positives.
    assert not re.search(rf"user_id={user_id}\b", reply_text), (
        f"DOM-D-07: 'user_id={user_id}' must not appear in /whoami reply"
    )


# ---------------------------------------------------------------------------
# Contract: unpair_command deletes pairing row
# ---------------------------------------------------------------------------


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_unpair_command_deletes_pairing(contract_conn):
    """unpair_command: DELETE FROM telegram_user_pairings is real.

    PTB boundary (reply_text) stays mocked.
    """
    from unittest.mock import patch

    from jarvis_common.testing import make_telegram_update
    from telegram_bot.handlers.commands.pairing_commands import unpair_command
    from telegram_bot.handlers.rate_limit import _timestamps

    _timestamps.clear()

    user_id = await _seed_user(contract_conn, "tg-contract-unpair@test.local")
    await contract_conn.execute(
        """INSERT INTO telegram_user_pairings (user_id, chat_id, telegram_username, paired_at)
           VALUES ($1, $2, $3, NOW())""",
        user_id,
        7701,
        "unpairuser",
    )

    pool = TgContractPool(contract_conn)
    update = make_telegram_update(chat_id=7701)
    context = _make_context(pool, args=[])

    with patch(
        "telegram_bot.handlers.commands._auth.auth_check",
        new_callable=AsyncMock,
        return_value=(True, user_id),
    ):
        await unpair_command(update, context)

    # Real DB assertion: pairing row must be gone
    row = await contract_conn.fetchrow(
        "SELECT user_id FROM telegram_user_pairings WHERE user_id = $1",
        user_id,
    )
    assert row is None, "telegram_user_pairings row must be deleted by unpair_command"

    # PTB assertion: "Unpaired" reply sent
    update.message.reply_text.assert_awaited_once()
    reply_text: str = update.message.reply_text.call_args[0][0]
    assert "Unpaired" in reply_text, f"Expected 'Unpaired' in reply; got: {reply_text!r}"


# ---------------------------------------------------------------------------
# Contract: pair_command rejects expired token (no pairing row created)
# ---------------------------------------------------------------------------


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_pair_command_rejects_expired_token(contract_conn):
    """pair_command: expired token leaves telegram_user_pairings untouched."""
    from jarvis_common.testing import make_telegram_update
    from telegram_bot.handlers.commands.pairing_commands import pair_command
    from telegram_bot.handlers.rate_limit import _timestamps

    _timestamps.clear()

    user_id = await _seed_user(contract_conn, "tg-contract-expired@test.local")
    # Seed an already-expired token
    token = await _seed_pairing_token(
        contract_conn,
        user_id,
        "contract-expired-token-001",
        expires_in=timedelta(minutes=-5),  # already expired
    )

    pool = TgContractPool(contract_conn)
    update = make_telegram_update(chat_id=5501)
    context = _make_context(pool, args=[token])

    await pair_command(update, context)

    # No pairing row should exist
    row = await contract_conn.fetchrow(
        "SELECT user_id FROM telegram_user_pairings WHERE user_id = $1",
        user_id,
    )
    assert row is None, "Expired token must not create a pairing row"

    # Expired token must have been deleted from telegram_pairing_tokens
    token_row = await contract_conn.fetchrow(
        "SELECT token FROM telegram_pairing_tokens WHERE token = $1",
        token,
    )
    assert token_row is None, "Expired token must be deleted on rejection"

    # PTB error reply
    update.message.reply_text.assert_awaited_once()
    reply_text: str = update.message.reply_text.call_args[0][0]
    assert "expired" in reply_text.lower(), f"Expected 'expired' in reply; got: {reply_text!r}"
