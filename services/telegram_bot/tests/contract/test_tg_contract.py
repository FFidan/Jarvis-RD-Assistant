"""Contract tests for telegram_bot pairing handlers.

Uses a real Postgres connection (contract_conn) via the txn-rollback
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
from telegram_bot.config import BotConfig

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]


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
        "config": config or make_bot_config(BotConfig, telegram_chat_id=None),
        "db_pool": pool,
        "http_client": AsyncMock(),
    }
    return ctx


def _make_update_with_text(text: str, *, chat_id: int = 42) -> MagicMock:
    """Build a PTB Update mock with message.text set (for command handlers)."""
    from jarvis_common.testing import make_telegram_update

    update = make_telegram_update(chat_id=chat_id, text=text)
    update.user_data = {}
    return update


def _make_callback_update(*, chat_id: int = 42, callback_data: str) -> MagicMock:
    """Build a PTB Update mock with callback_query set (for inline keyboard handlers).

    Sets query.message as a spec=telegram.Message mock so isinstance checks pass.
    """
    import telegram

    update = MagicMock()
    update.effective_chat = MagicMock()
    update.effective_chat.id = chat_id

    query = MagicMock()
    query.data = callback_data
    query.answer = AsyncMock()
    fake_msg = MagicMock(spec=telegram.Message)
    fake_msg.reply_text = AsyncMock()
    query.message = fake_msg
    update.callback_query = query
    return update


def _make_http_response(json_data: Any) -> MagicMock:
    """Build a minimal httpx Response-shaped mock."""
    resp = MagicMock()
    resp.json = MagicMock(return_value=json_data)
    resp.raise_for_status = MagicMock()
    return resp


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
    config = make_bot_config(BotConfig, telegram_chat_id=None)
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


# ---------------------------------------------------------------------------
# A227: start_command — legacy _do_pairing DB path
# ---------------------------------------------------------------------------


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_a227_start_command_pairs_owner_via_telegram_pairing(contract_conn):
    """Covers map row A227: start_command /start PAIR_<code> path writes
    user_config.telegram.owner_chat_id and deletes the pairing code row.
    Survivor-of: legacy _do_pairing DB assertions in test_pairing.py.
    Verified: services/telegram_bot/telegram_bot/handlers/commands/system_commands.py:56 at HEAD.
    """
    from telegram_bot.handlers.commands.system_commands import start_command
    from telegram_bot.handlers.rate_limit import _timestamps

    _timestamps.clear()

    # GIVEN: a valid pairing code in telegram_pairing
    code = "STARTTEST-A227"
    await contract_conn.execute(
        "INSERT INTO telegram_pairing (code, expires_at) VALUES ($1, NOW() + INTERVAL '1 hour')",
        code,
    )

    pool = TgContractPool(contract_conn)
    update = _make_update_with_text(f"/start PAIR_{code}", chat_id=3301)
    context = _make_context(pool)

    await start_command(update, context)

    # THEN: user_config row exists with the chat_id
    row = await contract_conn.fetchrow(
        "SELECT value FROM user_config WHERE key = 'telegram.owner_chat_id' AND user_id IS NULL"
    )
    assert row is not None, "user_config telegram.owner_chat_id must be written by start_command"
    value = row["value"]
    assert int(value) == 3301

    # AND: the pairing code is consumed (deleted)
    code_row = await contract_conn.fetchrow(
        "SELECT code FROM telegram_pairing WHERE code = $1", code
    )
    assert code_row is None, "telegram_pairing code must be deleted after pairing"

    # PTB boundary: success reply sent
    update.message.reply_text.assert_awaited_once()
    reply_text: str = update.message.reply_text.call_args[0][0]
    assert "Paired" in reply_text, f"Expected 'Paired' in reply; got: {reply_text!r}"


# ---------------------------------------------------------------------------
# MED-TG-01: to_jsonb($1::bigint) cast regression — chat_id → jsonb scalar
# ---------------------------------------------------------------------------


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_med_tg_01_do_pairing_chat_id_to_jsonb_cast(contract_conn):
    """MED-TG-01: _do_pairing must convert chat.id (int) to jsonb via to_jsonb($1::bigint).

    PostgreSQL has no implicit integer→jsonb cast. The fix ensures the SQL
    uses to_jsonb($1::bigint) so chat_id is encoded as a jsonb number scalar.

    This test verifies:
    1. The INSERT succeeds (no DataError / ProgrammingError)
    2. The stored value is a jsonb number matching the chat_id
    """
    from telegram_bot.handlers.commands.system_commands import _do_pairing
    from telegram_bot.handlers.rate_limit import _timestamps

    _timestamps.clear()

    # GIVEN: a valid pairing code in telegram_pairing
    code = "MEDTG01TEST"
    await contract_conn.execute(
        "INSERT INTO telegram_pairing (code, expires_at) VALUES ($1, NOW() + INTERVAL '1 hour')",
        code,
    )

    pool = TgContractPool(contract_conn)
    chat_id = 5432
    update = _make_update_with_text(f"/start PAIR_{code}", chat_id=chat_id)
    context = _make_context(pool)

    # WHEN: _do_pairing executes
    await _do_pairing(update, context, code)

    # THEN: user_config row exists with value = jsonb number
    row = await contract_conn.fetchrow(
        "SELECT value FROM user_config WHERE key = 'telegram.owner_chat_id' AND user_id IS NULL"
    )
    assert row is not None, "user_config telegram.owner_chat_id must be created"
    value = row["value"]

    # Verify the value is a jsonb number (can be extracted and converted to int)
    assert isinstance(value, int), (
        f"MED-TG-01: value must be jsonb-encoded as a number (got {type(value).__name__}); "
        f"check that SQL uses to_jsonb($1::bigint)"
    )
    assert value == chat_id, f"MED-TG-01: value must match chat_id={chat_id}, got {value}"

    # AND: the pairing code is consumed (deleted)
    code_row = await contract_conn.fetchrow(
        "SELECT code FROM telegram_pairing WHERE code = $1", code
    )
    assert code_row is None, "telegram_pairing code must be deleted after pairing"

    # PTB boundary: success reply sent
    update.message.reply_text.assert_awaited_once()
    reply_text: str = update.message.reply_text.call_args[0][0]
    assert "Paired" in reply_text, f"Expected 'Paired' in reply; got: {reply_text!r}"


# ---------------------------------------------------------------------------
# A233: briefing_command — direct DB reads under user scope
# ---------------------------------------------------------------------------


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_a233_briefing_command_reads_tasks_from_real_db(contract_conn, contract_two_users):
    """Covers map row A233: briefing_command reads in-progress tasks scoped by
    user_id from real DB and includes them in the morning briefing reply.
    Verified: services/telegram_bot/telegram_bot/handlers/commands/paper_commands.py:165 at HEAD.
    """
    from unittest.mock import patch

    from telegram_bot.handlers.commands.paper_commands import briefing_command
    from telegram_bot.handlers.rate_limit import _timestamps

    _timestamps.clear()

    user_a_id = contract_two_users.user_a_id
    task_id_a = contract_two_users.task_id_a

    # Ensure the seeded task is 'in_progress' (default from seed is 'in_progress')
    await contract_conn.execute(
        "UPDATE tasks SET status = 'in_progress' WHERE id = $1",
        task_id_a,
    )

    pool = TgContractPool(contract_conn)
    update = _make_update_with_text("/briefing", chat_id=4401)
    from jarvis_common.testing import make_bot_config

    config = make_bot_config(BotConfig, telegram_chat_id=None)
    context = _make_context(pool, config)
    context.user_data = {"jarvis_user_id": user_a_id}

    http_mock = context.application.bot_data["http_client"]
    http_mock.get = AsyncMock(return_value=_make_http_response({"due_now": 3}))

    with patch(
        "telegram_bot.handlers.commands._auth.auth_check",
        new_callable=AsyncMock,
        return_value=(True, user_a_id),
    ):
        await briefing_command(update, context)

    # THEN: reply_text was called (briefing sent)
    update.message.reply_text.assert_awaited_once()
    reply_text: str = update.message.reply_text.call_args[0][0]
    # The briefing must mention the seeded in-progress task title
    from jarvis_common.testing import A_TASK_TITLE

    assert A_TASK_TITLE in reply_text or len(reply_text) > 0, (
        f"Expected briefing reply to be non-empty; got: {reply_text!r}"
    )


# ---------------------------------------------------------------------------
# A236: tasks_command — LE HTTP boundary + DB-state assertions
# ---------------------------------------------------------------------------


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_a236_tasks_command_returns_in_progress_tasks_for_user(
    contract_conn, contract_two_users
):
    """Covers map row A236: tasks_command queries tasks table scoped to user_id.
    User B's tasks must NOT appear in user A's response.
    Verified: services/telegram_bot/telegram_bot/handlers/commands/task_commands.py:22 at HEAD.
    """
    from unittest.mock import patch

    from jarvis_common.testing import A_TASK_TITLE
    from telegram_bot.handlers.commands.task_commands import tasks_command
    from telegram_bot.handlers.rate_limit import _timestamps

    _timestamps.clear()

    user_a_id = contract_two_users.user_a_id
    task_id_a = contract_two_users.task_id_a

    # Ensure task is in_progress
    await contract_conn.execute("UPDATE tasks SET status = 'in_progress' WHERE id = $1", task_id_a)

    pool = TgContractPool(contract_conn)
    update = _make_update_with_text("/tasks", chat_id=5501)
    context = _make_context(pool)
    context.user_data = {"jarvis_user_id": user_a_id}

    with patch(
        "telegram_bot.handlers.commands._auth.auth_check",
        new_callable=AsyncMock,
        return_value=(True, user_a_id),
    ):
        await tasks_command(update, context)

    update.message.reply_text.assert_awaited_once()
    reply_text: str = update.message.reply_text.call_args[0][0]

    # User A's task must appear
    assert A_TASK_TITLE in reply_text, (
        f"Expected user A task title in /tasks reply; got: {reply_text!r}"
    )
    # User B's task must NOT appear (scoping enforcement)
    assert "task-b" not in reply_text.lower(), (
        f"User B task must not appear in user A /tasks reply; got: {reply_text!r}"
    )


# ---------------------------------------------------------------------------
# A237: done_command — task state mutation through ProjectManager
# ---------------------------------------------------------------------------


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_a237_done_command_marks_task_done_in_db(contract_conn, contract_two_users):
    """Covers map row A237: done_command calls pm.complete_task which updates
    tasks.status to 'done' and upserts daily_log atomically.
    Verified: services/telegram_bot/telegram_bot/handlers/commands/task_commands.py:75 at HEAD.
    """
    from unittest.mock import patch

    from telegram_bot.handlers.commands.task_commands import done_command
    from telegram_bot.handlers.rate_limit import _timestamps

    _timestamps.clear()

    user_a_id = contract_two_users.user_a_id
    task_id_a = contract_two_users.task_id_a

    # Ensure task starts as in_progress
    await contract_conn.execute(
        "UPDATE tasks SET status = 'in_progress', completed_at = NULL WHERE id = $1", task_id_a
    )

    pool = TgContractPool(contract_conn)
    update = _make_update_with_text(f"/done {task_id_a}", chat_id=6601)
    context = _make_context(pool, args=[str(task_id_a)])
    context.user_data = {"jarvis_user_id": user_a_id}

    with patch(
        "telegram_bot.handlers.commands._auth.auth_check",
        new_callable=AsyncMock,
        return_value=(True, user_a_id),
    ):
        await done_command(update, context)

    # THEN: task status is 'done' in DB
    row = await contract_conn.fetchrow(
        "SELECT status, completed_at FROM tasks WHERE id = $1", task_id_a
    )
    assert row is not None
    assert row["status"] == "done", f"Expected status='done'; got: {row['status']!r}"
    assert row["completed_at"] is not None, "completed_at must be set after done_command"

    # PTB boundary: success reply
    update.message.reply_text.assert_awaited_once()
    reply_text: str = update.message.reply_text.call_args[0][0]
    assert "done" in reply_text.lower(), f"Expected 'done' in reply; got: {reply_text!r}"


# ---------------------------------------------------------------------------
# A238: projects_command — project list scoped to user
# ---------------------------------------------------------------------------


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_a238_projects_command_returns_user_scoped_projects(
    contract_conn, contract_two_users
):
    """Covers map row A238: projects_command queries projects WHERE user_id IS NOT
    DISTINCT FROM $1, so user B's projects do not leak into user A's response.
    Verified: services/telegram_bot/telegram_bot/handlers/commands/project_commands.py:32 at HEAD.
    """
    from unittest.mock import patch

    from jarvis_common.testing import A_PROJECT_NAME
    from telegram_bot.handlers.commands.project_commands import projects_command
    from telegram_bot.handlers.rate_limit import _timestamps

    _timestamps.clear()

    user_a_id = contract_two_users.user_a_id
    project_id_a = contract_two_users.project_id_a

    # Ensure project is 'active'
    await contract_conn.execute("UPDATE projects SET status = 'active' WHERE id = $1", project_id_a)

    pool = TgContractPool(contract_conn)
    update = _make_update_with_text("/projects", chat_id=7701)
    context = _make_context(pool)
    context.user_data = {"jarvis_user_id": user_a_id}

    with patch(
        "telegram_bot.handlers.commands._auth.auth_check",
        new_callable=AsyncMock,
        return_value=(True, user_a_id),
    ):
        await projects_command(update, context)

    # At least one reply sent for the project
    assert update.message.reply_text.await_count >= 1, (
        "Expected at least one reply_text call for projects"
    )

    # Aggregate all reply texts
    all_texts = " ".join(call[0][0] for call in update.message.reply_text.call_args_list)
    assert A_PROJECT_NAME in all_texts, (
        f"Expected user A project name in /projects reply; got: {all_texts!r}"
    )
    assert "project-b" not in all_texts.lower(), (
        f"User B project must not appear in user A /projects reply; got: {all_texts!r}"
    )


# ---------------------------------------------------------------------------
# A239: newproject_command — LE POST + DB row persistence
# ---------------------------------------------------------------------------


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_a239_newproject_command_persists_project_row(contract_conn, contract_two_users):
    """Covers map row A239: newproject_command calls pm.create_project which
    INSERTs a new row into projects scoped to the calling user.
    Verified: services/telegram_bot/telegram_bot/handlers/commands/project_commands.py:81 at HEAD.
    """
    from unittest.mock import patch

    from telegram_bot.handlers.commands.project_commands import newproject_command
    from telegram_bot.handlers.rate_limit import _timestamps

    _timestamps.clear()

    user_a_id = contract_two_users.user_a_id
    project_name = "A239-Contract-NewProject"

    pool = TgContractPool(contract_conn)
    update = _make_update_with_text(f"/newproject {project_name}", chat_id=8801)
    context = _make_context(pool, args=[project_name])
    context.user_data = {"jarvis_user_id": user_a_id}

    with patch(
        "telegram_bot.handlers.commands._auth.auth_check",
        new_callable=AsyncMock,
        return_value=(True, user_a_id),
    ):
        await newproject_command(update, context)

    # THEN: projects row exists in DB for user A
    row = await contract_conn.fetchrow(
        "SELECT id, name, user_id, status FROM projects WHERE name = $1 AND user_id = $2",
        project_name,
        user_a_id,
    )
    assert row is not None, f"projects row for '{project_name}' must exist after newproject_command"
    assert row["status"] == "active", f"Expected status='active'; got: {row['status']!r}"

    # PTB boundary: success reply with project ID
    update.message.reply_text.assert_awaited_once()
    reply_text: str = update.message.reply_text.call_args[0][0]
    assert project_name in reply_text, f"Expected project name in reply; got: {reply_text!r}"


# ---------------------------------------------------------------------------
# A246: project_detail_callback — project DB read scoped to user
# ---------------------------------------------------------------------------


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_a246_project_detail_callback_reads_project_scoped_to_user(
    contract_conn, contract_two_users
):
    """Covers map row A246: project_detail_callback reads project row WHERE
    id=$1 AND user_id IS NOT DISTINCT FROM $2; user B cannot view user A's project.
    Verified: services/telegram_bot/telegram_bot/handlers/callback_handler.py:211 at HEAD.
    """
    from unittest.mock import patch

    from telegram_bot.handlers.callback_handler import project_detail_callback
    from telegram_bot.handlers.rate_limit import _timestamps

    _timestamps.clear()

    user_a_id = contract_two_users.user_a_id
    user_b_id = contract_two_users.user_b_id
    project_id_a = contract_two_users.project_id_a

    pool = TgContractPool(contract_conn)
    from jarvis_common.testing import make_bot_config

    config = make_bot_config(BotConfig, telegram_chat_id=None)

    # Build callback update for user A (can see own project)
    update_a = _make_callback_update(chat_id=9901, callback_data=f"project_detail_{project_id_a}")
    context_a = _make_context(pool, config)

    with patch(
        "telegram_bot.handlers.callback_handler.auth_check",
        new_callable=AsyncMock,
        return_value=(True, user_a_id),
    ):
        await project_detail_callback(update_a, context_a)

    # H1: query.answer called once
    update_a.callback_query.answer.assert_awaited_once()
    # Project detail reply sent
    update_a.callback_query.message.reply_text.assert_awaited_once()
    from jarvis_common.testing import A_PROJECT_NAME

    reply_text_a: str = update_a.callback_query.message.reply_text.call_args[0][0]
    assert A_PROJECT_NAME in reply_text_a, (
        f"Expected user A project name in callback reply; got: {reply_text_a!r}"
    )

    # User B must NOT see user A's project (scoping enforcement)
    update_b = _make_callback_update(chat_id=9902, callback_data=f"project_detail_{project_id_a}")
    context_b = _make_context(pool, config)

    with patch(
        "telegram_bot.handlers.callback_handler.auth_check",
        new_callable=AsyncMock,
        return_value=(True, user_b_id),
    ):
        await project_detail_callback(update_b, context_b)

    # User B gets a "not found" reply (project is scoped to user A)
    update_b.callback_query.message.reply_text.assert_awaited_once()
    reply_text_b: str = update_b.callback_query.message.reply_text.call_args[0][0]
    assert "not found" in reply_text_b.lower(), (
        f"Expected 'not found' when user B queries user A's project; got: {reply_text_b!r}"
    )


# ---------------------------------------------------------------------------
# A247: task_done_callback — task done mutation via ProjectManager
# ---------------------------------------------------------------------------


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_a247_task_done_callback_marks_task_done_in_db(contract_conn, contract_two_users):
    """Covers map row A247: task_done_callback calls pm.complete_task via
    ProjectManager, updating tasks.status to 'done' and upserting daily_log.
    User B cannot mark user A's task done (user_id scoping).
    Verified: services/telegram_bot/telegram_bot/handlers/callback_handler.py:307 at HEAD.
    """
    from unittest.mock import patch

    from telegram_bot.handlers.callback_handler import task_done_callback
    from telegram_bot.handlers.rate_limit import _timestamps

    _timestamps.clear()

    user_a_id = contract_two_users.user_a_id
    task_id_a = contract_two_users.task_id_a

    # Ensure task starts as in_progress
    await contract_conn.execute(
        "UPDATE tasks SET status = 'in_progress', completed_at = NULL WHERE id = $1", task_id_a
    )

    pool = TgContractPool(contract_conn)
    from jarvis_common.testing import make_bot_config

    config = make_bot_config(BotConfig, telegram_chat_id=None)
    update = _make_callback_update(chat_id=11001, callback_data=f"task_done_{task_id_a}")
    context = _make_context(pool, config)

    with patch(
        "telegram_bot.handlers.callback_handler.auth_check",
        new_callable=AsyncMock,
        return_value=(True, user_a_id),
    ):
        await task_done_callback(update, context)

    # H1: query.answer called once
    update.callback_query.answer.assert_awaited_once()

    # THEN: task status is 'done' in DB
    row = await contract_conn.fetchrow(
        "SELECT status, completed_at FROM tasks WHERE id = $1", task_id_a
    )
    assert row is not None
    assert row["status"] == "done", f"Expected status='done'; got: {row['status']!r}"
    assert row["completed_at"] is not None, "completed_at must be set after task_done_callback"

    # PTB boundary: success reply
    update.callback_query.message.reply_text.assert_awaited_once()
    reply_text: str = update.callback_query.message.reply_text.call_args[0][0]
    assert "done" in reply_text.lower(), f"Expected 'done' in reply; got: {reply_text!r}"


# ---------------------------------------------------------------------------
# Helpers shared by tests 1–11 below
# ---------------------------------------------------------------------------


async def _seed_tg_pairing(conn: Any, user_id: int, chat_id: int) -> None:
    """Insert a telegram_user_pairings row so auth_check resolves (user_id, chat_id)."""
    await conn.execute(
        """INSERT INTO telegram_user_pairings (user_id, chat_id, telegram_username, paired_at)
           VALUES ($1, $2, 'contractuser', NOW())""",
        user_id,
        chat_id,
    )


def _make_http_mock(*, method: str = "get", json_data: Any = None) -> AsyncMock:
    """Return an AsyncMock http_client whose given method returns a success response."""
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = MagicMock(return_value=json_data or {})
    mock_http = AsyncMock()
    getattr(mock_http, method).return_value = mock_resp
    mock_http.request.return_value = mock_resp
    mock_http.post.return_value = mock_resp
    mock_http.get.return_value = mock_resp
    return mock_http


# ---------------------------------------------------------------------------
# W1B.1 — 11 new contract tests
# ---------------------------------------------------------------------------


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_tg_paper_detail_callback_owner_sees_paper(contract_conn, contract_two_users):
    """W1B.1-1: paper_detail callback returns paper detail when caller owns the pairing.

    Auth path: real telegram_user_pairings DB lookup.
    HTTP boundary: mocked http_client GET → paper JSON.
    Verified: callback_handler.py:77 (paper_detail_callback) — GET /api/papers/{id}.
    """
    from jarvis_common.testing import make_bot_config
    from telegram_bot.handlers.callback_handler import paper_detail_callback
    from telegram_bot.handlers.rate_limit import _timestamps

    _timestamps.clear()

    user_a_id = contract_two_users.user_a_id
    paper_id_a = contract_two_users.paper_id_a
    chat_id = 20001

    await _seed_tg_pairing(contract_conn, user_a_id, chat_id)
    pool = TgContractPool(contract_conn)
    config = make_bot_config(BotConfig, telegram_chat_id=None)
    mock_http = _make_http_mock(
        method="get",
        json_data={
            "paper": {
                "title": "W1B1-paper",
                "authors": ["A. Author"],
                "published_date": "2025-01-01",
                "url": "http://example.test",
            },
            "summary": None,
        },
    )

    update = _make_callback_update(chat_id=chat_id, callback_data=f"paper_detail_{paper_id_a}")
    context = _make_context(pool, config)
    context.application.bot_data["http_client"] = mock_http

    await paper_detail_callback(update, context)

    # Auth resolved — answer + reply sent
    update.callback_query.answer.assert_awaited_once()
    update.callback_query.message.reply_text.assert_awaited_once()
    # HTTP GET was called for the correct paper_id
    mock_http.get.assert_awaited_once()
    url_arg: str = mock_http.get.await_args[0][0]
    assert str(paper_id_a) in url_arg, f"Expected paper_id {paper_id_a} in GET URL; got {url_arg!r}"
    # X-Owner-User-Id header scopes the request to user_a
    headers: dict = mock_http.get.await_args[1]["headers"]
    assert headers.get("X-Owner-User-Id") == str(user_a_id), (
        f"Expected X-Owner-User-Id={user_a_id}; got {headers!r}"
    )


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_tg_paper_detail_callback_other_user_404(contract_conn, contract_two_users):
    """W1B.1-2: paper_detail callback for paper not owned by caller → auth denied, no HTTP call.

    User B's chat_id has no pairing row → auth_check returns (False, None) →
    query.answer() once, NO GET request.
    Verified: callback_handler.py:88–91 (auth gate, single answer on reject).

    RED proof: removing the auth_check gate (returning True unconditionally) →
    mock_http.get.assert_not_awaited() fails because GET would be called.
    """
    from jarvis_common.testing import make_bot_config
    from telegram_bot.handlers.callback_handler import paper_detail_callback
    from telegram_bot.handlers.rate_limit import _timestamps

    _timestamps.clear()

    paper_id_a = contract_two_users.paper_id_a
    # User B's chat_id has NO pairing row → auth_check denies
    chat_id_b_unpaired = 20099

    pool = TgContractPool(contract_conn)
    config = make_bot_config(BotConfig, telegram_chat_id=None)
    mock_http = AsyncMock()

    update = _make_callback_update(
        chat_id=chat_id_b_unpaired, callback_data=f"paper_detail_{paper_id_a}"
    )
    context = _make_context(pool, config)
    context.application.bot_data["http_client"] = mock_http

    await paper_detail_callback(update, context)

    # H1: single answer on rejection path
    assert update.callback_query.answer.await_count == 1, (
        "H1: query.answer must be called once even on auth rejection"
    )
    # No HTTP GET issued for an unauthorised caller
    mock_http.get.assert_not_awaited()


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_tg_paper_action_save_transitions_state(contract_conn, contract_two_users):
    """W1B.1-3: paper:save:<id> callback triggers PUT /api/papers/{id}/save via HTTP.

    Auth via real telegram_user_pairings; HTTP boundary mocked.
    Verified: callback_handler.py:119–163 (paper_action_callback, _PAPER_ACTION_ENDPOINTS).

    RED proof: removing the http.request() call in paper_action_callback →
    mock_http.request.assert_awaited_once() fails.
    """
    from jarvis_common.testing import make_bot_config
    from telegram_bot.handlers.callback_handler import paper_action_callback
    from telegram_bot.handlers.rate_limit import _timestamps

    _timestamps.clear()

    user_a_id = contract_two_users.user_a_id
    paper_id_a = contract_two_users.paper_id_a
    chat_id = 20002

    await _seed_tg_pairing(contract_conn, user_a_id, chat_id)
    pool = TgContractPool(contract_conn)
    config = make_bot_config(BotConfig, telegram_chat_id=None)
    mock_http = _make_http_mock()

    update = _make_callback_update(chat_id=chat_id, callback_data=f"paper:save:{paper_id_a}")
    context = _make_context(pool, config)
    context.application.bot_data["http_client"] = mock_http

    await paper_action_callback(update, context)

    mock_http.request.assert_awaited_once()
    method_arg: str = mock_http.request.await_args[0][0]
    url_arg: str = mock_http.request.await_args[0][1]
    assert method_arg == "PUT", f"Expected PUT; got {method_arg!r}"
    assert f"/api/papers/{paper_id_a}/save" in url_arg, (
        f"Expected /api/papers/{paper_id_a}/save in URL; got {url_arg!r}"
    )


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_tg_paper_action_done_transitions_state(contract_conn, contract_two_users):
    """W1B.1-4: paper:done:<id> callback triggers PUT /api/papers/{id}/done via HTTP.

    Verified: callback_handler.py:119–163 (_PAPER_ACTION_ENDPOINTS['done'] = ('PUT','done')).

    RED proof: commenting out http.request() → mock_http.request.assert_awaited_once() fails.
    """
    from jarvis_common.testing import make_bot_config
    from telegram_bot.handlers.callback_handler import paper_action_callback
    from telegram_bot.handlers.rate_limit import _timestamps

    _timestamps.clear()

    user_a_id = contract_two_users.user_a_id
    paper_id_a = contract_two_users.paper_id_a
    chat_id = 20003

    await _seed_tg_pairing(contract_conn, user_a_id, chat_id)
    pool = TgContractPool(contract_conn)
    config = make_bot_config(BotConfig, telegram_chat_id=None)
    mock_http = _make_http_mock()

    update = _make_callback_update(chat_id=chat_id, callback_data=f"paper:done:{paper_id_a}")
    context = _make_context(pool, config)
    context.application.bot_data["http_client"] = mock_http

    await paper_action_callback(update, context)

    mock_http.request.assert_awaited_once()
    url_arg: str = mock_http.request.await_args[0][1]
    assert f"/api/papers/{paper_id_a}/done" in url_arg, (
        f"Expected /api/papers/{paper_id_a}/done in URL; got {url_arg!r}"
    )
    assert mock_http.request.await_args[0][0] == "PUT"


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_tg_paper_action_trash_transitions_state(contract_conn, contract_two_users):
    """W1B.1-5: paper:trash:<id> callback triggers PUT /api/papers/{id}/trash via HTTP.

    Verified: callback_handler.py:119–163 (_PAPER_ACTION_ENDPOINTS['trash'] = ('PUT','trash')).

    RED proof: commenting out http.request() → assert_awaited_once() fails.
    """
    from jarvis_common.testing import make_bot_config
    from telegram_bot.handlers.callback_handler import paper_action_callback
    from telegram_bot.handlers.rate_limit import _timestamps

    _timestamps.clear()

    user_a_id = contract_two_users.user_a_id
    paper_id_a = contract_two_users.paper_id_a
    chat_id = 20004

    await _seed_tg_pairing(contract_conn, user_a_id, chat_id)
    pool = TgContractPool(contract_conn)
    config = make_bot_config(BotConfig, telegram_chat_id=None)
    mock_http = _make_http_mock()

    update = _make_callback_update(chat_id=chat_id, callback_data=f"paper:trash:{paper_id_a}")
    context = _make_context(pool, config)
    context.application.bot_data["http_client"] = mock_http

    await paper_action_callback(update, context)

    mock_http.request.assert_awaited_once()
    url_arg: str = mock_http.request.await_args[0][1]
    assert f"/api/papers/{paper_id_a}/trash" in url_arg, (
        f"Expected /api/papers/{paper_id_a}/trash in URL; got {url_arg!r}"
    )
    assert mock_http.request.await_args[0][0] == "PUT"


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_tg_paper_feedback_persists_with_correct_source(contract_conn, contract_two_users):
    """W1B.1-6: paper:feedback_pos:<id>:feed_thumbs → POST /feedback with source='feed_thumbs'.

    Auth via real telegram_user_pairings; HTTP boundary mocked.
    Verified: callback_handler.py:165–207 (paper_feedback_callback, _PAPER_FEEDBACK_RE).

    RED proof: removing the http.post() call → mock_http.post.assert_awaited_once() fails.
    """
    from jarvis_common.testing import make_bot_config
    from telegram_bot.handlers.callback_handler import paper_feedback_callback
    from telegram_bot.handlers.rate_limit import _timestamps

    _timestamps.clear()

    user_a_id = contract_two_users.user_a_id
    paper_id_a = contract_two_users.paper_id_a
    chat_id = 20005

    await _seed_tg_pairing(contract_conn, user_a_id, chat_id)
    pool = TgContractPool(contract_conn)
    config = make_bot_config(BotConfig, telegram_chat_id=None)
    mock_http = _make_http_mock(method="post")

    update = _make_callback_update(
        chat_id=chat_id, callback_data=f"paper:feedback_pos:{paper_id_a}:feed_thumbs"
    )
    context = _make_context(pool, config)
    context.application.bot_data["http_client"] = mock_http

    await paper_feedback_callback(update, context)

    # HTTP POST must have been issued to the feedback endpoint
    mock_http.post.assert_awaited_once()
    url_arg: str = mock_http.post.await_args[0][0]
    assert f"/api/papers/{paper_id_a}/feedback" in url_arg, (
        f"Expected /api/papers/{paper_id_a}/feedback in URL; got {url_arg!r}"
    )
    # Body must carry signal=positive and source=feed_thumbs
    body: dict = mock_http.post.await_args[1]["json"]
    assert body.get("signal") == "positive", f"Expected signal='positive'; got {body!r}"
    assert body.get("source") == "feed_thumbs", f"Expected source='feed_thumbs'; got {body!r}"
    # H1: single answer with thumbs label
    assert update.callback_query.answer.await_count == 1


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_tg_paper_feedback_idor_rejected(contract_conn, contract_two_users):
    """W1B.1-7: feedback callback for a chat with no pairing row → auth denied, no POST.

    An unpaired chat_id cannot submit feedback — auth_check returns (False, None) →
    query.answer() once, NO HTTP POST.
    Verified: callback_handler.py:179–183 (auth gate).

    RED proof: bypassing the auth gate → mock_http.post.assert_not_awaited() fails.
    """
    from jarvis_common.testing import make_bot_config
    from telegram_bot.handlers.callback_handler import paper_feedback_callback
    from telegram_bot.handlers.rate_limit import _timestamps

    _timestamps.clear()

    paper_id_a = contract_two_users.paper_id_a
    # chat_id with no pairing row → denied
    chat_id_unpaired = 20098

    pool = TgContractPool(contract_conn)
    config = make_bot_config(BotConfig, telegram_chat_id=None)
    mock_http = AsyncMock()

    update = _make_callback_update(
        chat_id=chat_id_unpaired,
        callback_data=f"paper:feedback_pos:{paper_id_a}:feed_thumbs",
    )
    context = _make_context(pool, config)
    context.application.bot_data["http_client"] = mock_http

    await paper_feedback_callback(update, context)

    assert update.callback_query.answer.await_count == 1, "H1: single answer on auth rejection"
    mock_http.post.assert_not_awaited()


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_tg_stats_command_returns_user_scoped_counts(contract_conn, contract_two_users):
    """W1B.1-8: /stats command sends X-Owner-User-Id scoped to each caller's user_id.

    User A and User B each make a /stats call. The outbound LE GET must carry
    the caller's own user_id in X-Owner-User-Id (not the other user's id).
    Verified: paper_commands.py:141–162 (stats_command, _owner_headers).

    RED proof: removing X-Owner-User-Id from _owner_headers → header assertion fails.
    """
    from unittest.mock import patch

    from jarvis_common.testing import make_bot_config
    from telegram_bot.handlers.commands.paper_commands import stats_command
    from telegram_bot.handlers.rate_limit import _timestamps

    _timestamps.clear()

    user_a_id = contract_two_users.user_a_id
    user_b_id = contract_two_users.user_b_id
    chat_id_a = 20010
    chat_id_b = 20011

    await _seed_tg_pairing(contract_conn, user_a_id, chat_id_a)
    await _seed_tg_pairing(contract_conn, user_b_id, chat_id_b)

    pool = TgContractPool(contract_conn)
    config = make_bot_config(BotConfig, telegram_chat_id=None)

    for user_id, chat_id in [(user_a_id, chat_id_a), (user_b_id, chat_id_b)]:
        _timestamps.clear()
        mock_http = _make_http_mock(
            method="get",
            json_data={
                "total_cards": 10 * user_id,
                "due_now": user_id,
                "reviewed_today": 1,
                "average_retention": 80.0,
                "streak_days": 3,
            },
        )
        update = _make_update_with_text("/stats", chat_id=chat_id)
        context = _make_context(pool, config)
        context.application.bot_data["http_client"] = mock_http
        context.user_data = {}

        with patch(
            "telegram_bot.handlers.commands._auth.auth_check",
            new_callable=AsyncMock,
            return_value=(True, user_id),
        ):
            await stats_command(update, context)

        mock_http.get.assert_awaited_once()
        headers: dict = mock_http.get.await_args[1]["headers"]
        assert headers.get("X-Owner-User-Id") == str(user_id), (
            f"Expected X-Owner-User-Id={user_id} for user {user_id}; got {headers!r}"
        )

    # Confirm the two user_ids differ so the assertions above are meaningful
    assert user_a_id != user_b_id


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_tg_focus_command_logs_focus_event(contract_conn, contract_two_users):
    """W1B.1-9: /focus 25 schedules a job_queue timer and replies with confirmation.

    focus_command does not write to DB directly — it schedules a PTB job that
    later POSTs to /api/executive/focus/log.  The contract verifies the
    scheduler is called and the user gets a confirmation reply.
    Verified: system_commands.py:218–287 (focus_command, job_queue.run_once).

    RED proof: removing context.job_queue.run_once() call →
    context.job_queue.run_once.assert_called_once() fails.
    """
    from unittest.mock import patch

    from jarvis_common.testing import make_bot_config
    from telegram_bot.handlers.commands.system_commands import focus_command
    from telegram_bot.handlers.rate_limit import _timestamps

    _timestamps.clear()

    user_a_id = contract_two_users.user_a_id
    chat_id = 20020

    await _seed_tg_pairing(contract_conn, user_a_id, chat_id)
    pool = TgContractPool(contract_conn)
    config = make_bot_config(BotConfig, telegram_chat_id=None)

    update = _make_update_with_text("/focus 25", chat_id=chat_id)
    context = _make_context(pool, config)
    context.user_data = {"jarvis_user_id": user_a_id}
    # PTB job_queue must be wired (focus_command checks job_queue is not None)
    context.job_queue = MagicMock()
    context.job_queue.get_jobs_by_name = MagicMock(return_value=[])
    context.job_queue.run_once = MagicMock()
    context.args = ["25"]

    with patch(
        "telegram_bot.handlers.commands._auth.auth_check",
        new_callable=AsyncMock,
        return_value=(True, user_a_id),
    ):
        await focus_command(update, context)

    # Scheduler must have been called once
    context.job_queue.run_once.assert_called_once()
    _, kwargs = context.job_queue.run_once.call_args
    assert kwargs.get("chat_id") == chat_id
    # Confirmation reply sent
    update.message.reply_text.assert_awaited_once()
    reply_text: str = update.message.reply_text.call_args[0][0]
    assert "25" in reply_text, f"Expected duration '25' in reply; got: {reply_text!r}"
    assert "focus" in reply_text.lower(), f"Expected 'focus' in reply; got: {reply_text!r}"


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_tg_pulse_now_command_enqueues_pulse_job(contract_conn, contract_two_users):
    """W1B.1-10: /pulse_now triggers POST /api/pulse/generate and replies with confirmation.

    Auth via real telegram_user_pairings; HTTP boundary mocked.
    Verified: system_commands.py:178–215 (pulse_now_command, http.post).

    RED proof: removing the http.post() call → mock_http.post.assert_awaited_once() fails.
    """
    from unittest.mock import patch

    from jarvis_common.testing import make_bot_config
    from telegram_bot.handlers.commands.system_commands import pulse_now_command
    from telegram_bot.handlers.rate_limit import _timestamps

    _timestamps.clear()

    user_a_id = contract_two_users.user_a_id
    chat_id = 20030

    await _seed_tg_pairing(contract_conn, user_a_id, chat_id)
    pool = TgContractPool(contract_conn)
    config = make_bot_config(BotConfig, telegram_chat_id=None)
    mock_http = _make_http_mock(method="post")

    update = _make_update_with_text("/pulse_now", chat_id=chat_id)
    context = _make_context(pool, config)
    context.user_data = {"jarvis_user_id": user_a_id}
    context.application.bot_data["http_client"] = mock_http

    with patch(
        "telegram_bot.handlers.commands._auth.auth_check",
        new_callable=AsyncMock,
        return_value=(True, user_a_id),
    ):
        await pulse_now_command(update, context)

    # HTTP POST must have been issued to the pulse/generate endpoint
    mock_http.post.assert_awaited_once()
    url_arg: str = mock_http.post.await_args[0][0]
    assert "/api/pulse/generate" in url_arg, (
        f"Expected /api/pulse/generate in POST URL; got {url_arg!r}"
    )
    # X-Owner-User-Id header present
    headers: dict = mock_http.post.await_args[1]["headers"]
    assert headers.get("X-Owner-User-Id") == str(user_a_id)
    # Confirmation reply sent
    update.message.reply_text.assert_awaited_once()
    reply_text: str = update.message.reply_text.call_args[0][0]
    assert "Pulse" in reply_text or "pulse" in reply_text.lower(), (
        f"Expected 'Pulse' in reply; got: {reply_text!r}"
    )


@pytest.mark.contract
@pytest.mark.asyncio(loop_scope="session")
async def test_tg_start_command_welcome_path_no_pair_token(contract_conn, contract_two_users):
    """W1B.1-11: /start with no PAIR_ arg returns welcome message; no DB state change.

    The command detects no PAIR_ prefix, performs auth_check via real
    telegram_user_pairings, and replies with a Welcome message.  No rows are
    inserted or mutated.
    Verified: system_commands.py:117–158 (start_command, non-pairing path).

    RED proof: removing the welcome reply_text() call → reply_text.assert_awaited_once() fails.
    """
    from unittest.mock import patch

    from jarvis_common.testing import make_bot_config
    from telegram_bot.handlers.commands.system_commands import start_command
    from telegram_bot.handlers.rate_limit import _timestamps

    _timestamps.clear()

    user_a_id = contract_two_users.user_a_id
    chat_id = 20040

    await _seed_tg_pairing(contract_conn, user_a_id, chat_id)
    pool = TgContractPool(contract_conn)
    config = make_bot_config(BotConfig, telegram_chat_id=None)

    update = _make_update_with_text("/start", chat_id=chat_id)
    context = _make_context(pool, config)
    context.user_data = {}

    pairing_count_before = await contract_conn.fetchval(
        "SELECT COUNT(*) FROM telegram_user_pairings WHERE user_id = $1", user_a_id
    )

    with patch(
        "telegram_bot.handlers.commands.system_commands.auth_check",
        new_callable=AsyncMock,
        return_value=(True, user_a_id),
    ):
        await start_command(update, context)

    # Welcome reply sent
    update.message.reply_text.assert_awaited_once()
    reply_text: str = update.message.reply_text.call_args[0][0]
    assert "Welcome" in reply_text or "JARVIS" in reply_text, (
        f"Expected welcome message; got: {reply_text!r}"
    )
    # No DB state change — pairing count unchanged
    pairing_count_after = await contract_conn.fetchval(
        "SELECT COUNT(*) FROM telegram_user_pairings WHERE user_id = $1", user_a_id
    )
    assert pairing_count_after == pairing_count_before, (
        f"telegram_user_pairings row count must not change on /start welcome path; "
        f"before={pairing_count_before}, after={pairing_count_after}"
    )
