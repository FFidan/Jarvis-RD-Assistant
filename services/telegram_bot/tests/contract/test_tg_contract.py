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
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import httpx
import pytest
from fastapi import FastAPI
from jarvis_common.identity_capabilities import ServicePrincipal
from jarvis_common.testing import (
    PTBContextOptions,
    SharedConnPool,
    make_ptb_context,
    seed_user_row,
)
from telegram_bot.config import BotConfig

pytestmark = [
    pytest.mark.contract,
    pytest.mark.real_auth,
    pytest.mark.asyncio(loop_scope="session"),
]


# ---------------------------------------------------------------------------
# Platform contract client
# ---------------------------------------------------------------------------
# The bot remains database-free: every pairing operation traverses the real
# Platform HTTP router. Platform alone receives the rollback-scoped connection.


class TgContractPlatformClient:
    """HTTP-shaped client backed by Platform's real Telegram router.

    Parameters
    ----------
    conn : Any
        Rollback-scoped PostgreSQL contract connection owned by Platform.
    """

    def __init__(self, conn: Any) -> None:
        from platform_api.deps import authenticate_service_principal, get_db_pool
        from platform_api.routers import internal_telegram

        shared = SharedConnPool(conn)
        app = FastAPI()
        app.include_router(internal_telegram.router)

        def principal_override() -> ServicePrincipal:
            return "telegram"

        def pool_override() -> asyncpg.Pool:
            return cast(asyncpg.Pool, shared)

        app.dependency_overrides[authenticate_service_principal] = principal_override
        app.dependency_overrides[get_db_pool] = pool_override
        self._app = app

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Dispatch one request through the in-process Platform boundary."""
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self._app),
            base_url="http://platform:8003",
        ) as client:
            return await client.request(method, url, **kwargs)

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        """Dispatch a GET request to Platform."""
        return await self._request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        """Dispatch a POST request to Platform."""
        return await self._request("POST", url, **kwargs)

    async def delete(self, url: str, **kwargs: Any) -> httpx.Response:
        """Dispatch a DELETE request to Platform."""
        return await self._request("DELETE", url, **kwargs)


# ---------------------------------------------------------------------------
# DB seeding helpers
# ---------------------------------------------------------------------------


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


def _build_context(
    platform_client: Any,
    config: Any = None,
    *,
    args: list[str] | None = None,
) -> MagicMock:
    """Build a PTB context wired to the in-process Platform client."""
    from jarvis_common.testing import make_bot_config

    return make_ptb_context(
        platform_client,
        config or make_bot_config(BotConfig),
        options=PTBContextOptions(args=args, with_bot=True),
    )


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
    update.effective_chat.type = "private"

    query = MagicMock()
    query.data = callback_data
    query.answer = AsyncMock()
    fake_msg = MagicMock(spec=telegram.Message)
    fake_msg.reply_text = AsyncMock()
    query.message = fake_msg
    update.callback_query = query
    return update


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

    user_id = await seed_user_row(contract_conn, "tg-contract-pair@test.local")
    token = await _seed_pairing_token(contract_conn, user_id, "contract-pair-token-001")

    platform_client = TgContractPlatformClient(contract_conn)
    update = make_telegram_update(chat_id=8801, username="contractuser")
    context = _build_context(platform_client, args=[token])

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

    user_id = await seed_user_row(contract_conn, "tg-contract-whoami@test.local")
    # Insert a pairing row directly (bypass pair_command for isolation)
    await contract_conn.execute(
        """INSERT INTO telegram_user_pairings (user_id, chat_id, telegram_username, paired_at)
           VALUES ($1, $2, $3, NOW())""",
        user_id,
        9901,
        "whoamiuser",
    )

    platform_client = TgContractPlatformClient(contract_conn)
    update = make_telegram_update(chat_id=9901)
    #
    config = make_bot_config(BotConfig)
    context = _build_context(platform_client, config=config)

    await whoami_command(update, context)

    update.message.reply_text.assert_awaited_once()
    reply_text: str = update.message.reply_text.call_args[0][0]
    assert "Paired" in reply_text, f"Expected 'Paired' in /whoami reply; got: {reply_text!r}"
    # Date of pairing must appear (real paired_at from DB)
    import re

    assert re.search(r"\d{4}-\d{2}-\d{2}", reply_text), (
        f"Expected paired-at date in /whoami reply; got: {reply_text!r}"
    )
    # Raw DB PK must not leak as a standalone identifier.
    # Use a regex word-boundary check so incidental digit overlaps with dates
    # (e.g. user_id=2 inside "2026-05-21") do not produce false positives.
    assert not re.search(rf"user_id={user_id}\b", reply_text), (
        f"'user_id={user_id}' must not appear in /whoami reply"
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

    user_id = await seed_user_row(contract_conn, "tg-contract-unpair@test.local")
    await contract_conn.execute(
        """INSERT INTO telegram_user_pairings (user_id, chat_id, telegram_username, paired_at)
           VALUES ($1, $2, $3, NOW())""",
        user_id,
        7701,
        "unpairuser",
    )

    platform_client = TgContractPlatformClient(contract_conn)
    update = make_telegram_update(chat_id=7701)
    context = _build_context(platform_client, args=[])

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

    user_id = await seed_user_row(contract_conn, "tg-contract-expired@test.local")
    # Seed an already-expired token
    token = await _seed_pairing_token(
        contract_conn,
        user_id,
        "contract-expired-token-001",
        expires_in=timedelta(minutes=-5),  # already expired
    )

    platform_client = TgContractPlatformClient(contract_conn)
    update = make_telegram_update(chat_id=5501)
    context = _build_context(platform_client, args=[token])

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
# A233/A236/A237/A238/A239/A246/A247/A247b — DELETED in the Telegram→REST
# decoupling (T15).
#
# These were DB-state contract tests: they seeded a real DB and asserted DB
# state (task status, project rows) after invoking a product handler.  Those
# handlers now route through the service REST API (services_client) instead of
# writing the DB directly, so the DB-state assertions are obsolete.  Their two
# guarantees are now covered elsewhere:
#
#   (a) "the bot issues the right REST call (URL + X-Owner-User-Id) and renders
#       correctly" — covered by the http-mock unit tests in
#       services/telegram_bot/tests/test_command_handlers.py and
#       test_callback_handlers.py:
#         A233 briefing  → test_briefing_returns_text,
#                          test_briefing_scopes_to_user_via_owner_header_when_paired
#         A236 /tasks    → test_tasks_scopes_query_to_paired_user,
#                          test_tasks_scopes_query_with_project_filter
#         A237 /done     → test_done_success, test_done_passes_user_id_via_owner_header
#         A238 /projects → test_projects_with_data, test_projects_scopes_listing_to_paired_user
#         A239 /newproject → test_newproject_success,
#                          test_newproject_passes_user_id_via_owner_header
#         A246 project_detail → test_project_detail_success, test_project_detail_not_found
#         A247 task_done → test_task_done_success,
#                          test_task_done_forwards_owner_user_id_for_paired_user
#
#   (b) the service-side DB write / cross-tenant enforcement — covered by the
#       learning_engine contract tests (real live-PG):
#         A236 list-scoping → test_tasks_contract.py::test_list_tasks_owner_sees_own_task,
#                          test_list_tasks_non_owner_project_gets_404
#         A237 done+daily_log → test_tasks_contract.py::test_status_to_done_increments_daily_log
#                          (+ second/redone/coalesce variants)
#         A238/A239 project scope+insert → test_projects_contract.py::
#                          test_create_project_row_has_caller_user_id,
#                          test_create_project_absent_from_user_b_list
#         A246 project cross-tenant → test_projects_contract.py::
#                          test_get_project_cross_tenant_returns_404
#         A247/A247b → test_tasks_contract.py::
#                          test_update_task_user_b_gets_404,
#                          test_update_task_cross_tenant_returns_404 (404 + row unchanged),
#                          plus bot-half test_callback_handlers.py::
#                          test_task_done_non_owned_task_returns_not_found_no_leak.
#
# The old A247b forced auth_check → (True, None) to
# exercise a consumer-side ``$2 IS NULL`` catch-all in complete_task.  That
# direct-DB path no longer exists in the bot — the handler PUTs to the LE, which
# enforces ownership via X-Owner-User-Id and returns 404 for non-owned tasks
# (test_update_task_cross_tenant_returns_404 proves the row stays unchanged;
# test_task_done_non_owned_task_returns_not_found_no_leak proves the bot renders
# "not found" with no existence leak).
# ---------------------------------------------------------------------------


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
    platform_client = TgContractPlatformClient(contract_conn)
    config = make_bot_config(BotConfig)
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
    context = _build_context(platform_client, config)
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

    platform_client = TgContractPlatformClient(contract_conn)
    config = make_bot_config(BotConfig)
    mock_http = AsyncMock()

    update = _make_callback_update(
        chat_id=chat_id_b_unpaired, callback_data=f"paper_detail_{paper_id_a}"
    )
    context = _build_context(platform_client, config)
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
    platform_client = TgContractPlatformClient(contract_conn)
    config = make_bot_config(BotConfig)
    mock_http = _make_http_mock()

    update = _make_callback_update(chat_id=chat_id, callback_data=f"paper:save:{paper_id_a}")
    context = _build_context(platform_client, config)
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
    platform_client = TgContractPlatformClient(contract_conn)
    config = make_bot_config(BotConfig)
    mock_http = _make_http_mock()

    update = _make_callback_update(chat_id=chat_id, callback_data=f"paper:done:{paper_id_a}")
    context = _build_context(platform_client, config)
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
    platform_client = TgContractPlatformClient(contract_conn)
    config = make_bot_config(BotConfig)
    mock_http = _make_http_mock()

    update = _make_callback_update(chat_id=chat_id, callback_data=f"paper:trash:{paper_id_a}")
    context = _build_context(platform_client, config)
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
    platform_client = TgContractPlatformClient(contract_conn)
    config = make_bot_config(BotConfig)
    mock_http = _make_http_mock(method="post")

    update = _make_callback_update(
        chat_id=chat_id, callback_data=f"paper:feedback_pos:{paper_id_a}:feed_thumbs"
    )
    context = _build_context(platform_client, config)
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

    platform_client = TgContractPlatformClient(contract_conn)
    config = make_bot_config(BotConfig)
    mock_http = AsyncMock()

    update = _make_callback_update(
        chat_id=chat_id_unpaired,
        callback_data=f"paper:feedback_pos:{paper_id_a}:feed_thumbs",
    )
    context = _build_context(platform_client, config)
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

    platform_client = TgContractPlatformClient(contract_conn)
    config = make_bot_config(BotConfig)

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
        context = _build_context(platform_client, config)
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
async def test_tg_focus_command_starts_scoped_durable_session(contract_conn, contract_two_users):
    """/focus starts the shared server interval and creates no process-local timer."""
    from unittest.mock import patch

    from jarvis_common.testing import make_bot_config
    from telegram_bot.handlers.commands.system_commands import focus_command
    from telegram_bot.handlers.rate_limit import _timestamps

    _timestamps.clear()

    user_a_id = contract_two_users.user_a_id
    chat_id = 20020

    await _seed_tg_pairing(contract_conn, user_a_id, chat_id)
    platform_client = TgContractPlatformClient(contract_conn)
    config = make_bot_config(BotConfig)

    update = _make_update_with_text("/focus 25", chat_id=chat_id)
    context = _build_context(platform_client, config)
    context.user_data = {"jarvis_user_id": user_a_id}
    context.job_queue = MagicMock()
    context.job_queue.run_once = MagicMock()
    context.args = ["25"]
    mock_http = _make_http_mock(
        method="post",
        json_data={
            "id": 81,
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
        },
    )
    context.application.bot_data["http_client"] = mock_http

    with patch(
        "telegram_bot.handlers.commands._auth.auth_check",
        new_callable=AsyncMock,
        return_value=(True, user_a_id),
    ):
        await focus_command(update, context)

    context.job_queue.run_once.assert_not_called()
    mock_http.post.assert_awaited_once()
    _, kwargs = mock_http.post.await_args
    assert kwargs["json"] == {"duration_seconds": 1500, "source": "telegram"}
    assert kwargs["headers"]["X-Owner-User-Id"] == str(user_a_id)
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
    platform_client = TgContractPlatformClient(contract_conn)
    config = make_bot_config(BotConfig)
    mock_http = _make_http_mock(method="post")

    update = _make_update_with_text("/pulse_now", chat_id=chat_id)
    context = _build_context(platform_client, config)
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
    """W1B.1-11: /start from a paired chat returns the welcome message; no DB state change.

    The command performs auth_check via real telegram_user_pairings and replies
    with a Welcome message.  No rows are inserted or mutated.
    Verified: system_commands.py start_command at HEAD.

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
    platform_client = TgContractPlatformClient(contract_conn)
    config = make_bot_config(BotConfig)

    update = _make_update_with_text("/start", chat_id=chat_id)
    context = _build_context(platform_client, config)
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
