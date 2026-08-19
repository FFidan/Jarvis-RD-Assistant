"""Live-backend execution harness for Telegram bot command handlers.

Why this module exists
----------------------
Every other handler test in this service replaces the HTTP layer with a mock or
an in-process ASGI app. That layer can hide a dead contract: a handler may keep
sending a request shape a backend already stopped accepting, and the mocked
suite stays green because the mock answers whatever the test taught it to
answer. No existing module owns handler execution against a *running* backend,
so this file adds that layer rather than extending one.

What it does
------------
It reuses the synthesized ``Update`` / ``Context`` pattern of
``test_command_handlers.py`` but wires the context to the same real clients
``telegram_bot.main`` builds at startup: a Platform service client and a
downstream client carrying :class:`~telegram_bot.service_auth.TelegramBackendAuth`.
Handlers therefore issue genuine HTTP requests to the Research and Learning
services, mint genuine Platform assertions, and parse genuine responses.

Only the bot-side authorization decision is patched (``auth_check``), exactly as
the unit suite patches it, so that a synthesized chat id resolves to a known
paired user. Nothing on the HTTP path is replaced. When a credential or a
service is missing the module skips with a precise reason; it never falls back
to a mock, because a mocked fallback would recreate the defect class this
module exists to kill.

Running it
----------
The module is excluded from the ordinary suite by the ``live_backend`` marker
(see the root ``addopts`` deselection). Opt in with::

    JARVIS_RUN_LIVE_BACKEND=1 \\
    PLATFORM_API_URL=http://127.0.0.1:<platform-port> \\
    PAPER_INGESTION_URL=http://127.0.0.1:<research-port> \\
    LEARNING_ENGINE_URL=http://127.0.0.1:<learning-port> \\
    JARVIS_TELEGRAM_SERVICE_TOKEN_FILE=<deployment>/secrets/telegram_service_token.txt \\
    uv run pytest -m live_backend services/telegram_bot/tests/test_live_handlers.py

``JARVIS_LIVE_BACKEND_USER_ID`` (default ``1``) selects the paired JARVIS user
whose data the handlers read. The service origins must be the ones the running
deployment actually serves; the outbound policy of ``pinned_async_client``
permits the Compose service names and loopback only.

Assertions are deliberately about *shape*, never about values that vary per
deployment: header lines, keyboard geometry, callback-data prefixes, and the
absence of the handlers' own degraded-path replies.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from jarvis_common.pinned_transport import JARVIS_SERVICE_POLICY, pinned_async_client
from jarvis_common.secrets_files import read_secret_with_file_fallback
from jarvis_common.testing_telegram import PTBContextOptions, make_ptb_context
from pydantic import SecretStr
from telegram_bot.config import BotConfig, service_headers
from telegram_bot.handlers.commands import briefing_command, tasks_command
from telegram_bot.handlers.commands.paper_commands import (
    discover_command,
    next_command,
    papers_command,
    stats_command,
)
from telegram_bot.handlers.commands.system_commands import pulse_now_command
from telegram_bot.service_auth import TelegramBackendAuth

pytestmark = [
    pytest.mark.live_backend,
    pytest.mark.usefixtures("_clear_rate_limit_state"),
]

# ---------------------------------------------------------------------------
# Opt-in gate
# ---------------------------------------------------------------------------

#: Environment opt-in. Mirrors the ``JARVIS_RUN_LIVE_PG`` convention used by the
#: Postgres-backed suites.
_OPT_IN_ENV = "JARVIS_RUN_LIVE_BACKEND"

#: Chat id of the synthesized private chat. Never reaches the network: the
#: Telegram Bot API boundary stays a test double, only the backend is live.
_LIVE_CHAT_ID = 424242

#: Second opt-in for the one handler that changes deployment state. ``/pulse_now``
#: starts a real Pulse run: minutes of LLM work against a rate limit of three
#: per hour. Reading the deck changes nothing, so it stays separate.
_GENERATE_OPT_IN_ENV = "JARVIS_RUN_LIVE_PULSE_GENERATE"

if os.environ.get(_OPT_IN_ENV) != "1":
    pytest.skip(
        f"live-backend handler harness requires {_OPT_IN_ENV}=1",
        allow_module_level=True,
    )


# ---------------------------------------------------------------------------
# Live wiring
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LiveBackend:
    """Real clients and identity a handler needs to reach a running deployment.

    Parameters
    ----------
    config : BotConfig
        Configuration whose service origins point at the running deployment and
        whose service credential came from the environment or a mounted secret.
    http : httpx.AsyncClient
        Downstream client carrying ``TelegramBackendAuth``; this is the object
        handlers read out of ``context.application.bot_data["http_client"]``.
    user_id : int
        Paired JARVIS user whose data the handlers read.
    """

    config: BotConfig
    http: httpx.AsyncClient
    user_id: int


def _live_bot_config() -> BotConfig:
    """Build a bot configuration for the running deployment.

    Service origins come from the ordinary ``BotConfig`` environment fields; the
    Telegram service credential is resolved the way production resolves it.

    Returns
    -------
    BotConfig
        Configuration carrying the deployment's service credential.

    Notes
    -----
    ``BotConfig.from_env`` is not used: it additionally demands a Telegram Bot
    API token and exits the process when one is absent, which is irrelevant here
    because the Telegram API boundary is never dialled.
    """
    # BotConfig reads the three service origins from the environment.
    config = BotConfig()
    # Direct value first, mounted secret file second.
    token = read_secret_with_file_fallback(
        config.telegram_service_token.get_secret_value() or None,
        os.environ.get("JARVIS_TELEGRAM_SERVICE_TOKEN_FILE", ""),
    )
    if not token:
        pytest.skip(
            "no Telegram service credential: set JARVIS_TELEGRAM_SERVICE_TOKEN or "
            "JARVIS_TELEGRAM_SERVICE_TOKEN_FILE; this harness refuses to mock it"
        )
    return config.model_copy(update={"telegram_service_token": SecretStr(token)})


async def _require_healthy(url: str, label: str) -> None:
    """Skip unless a service answers its health endpoint.

    Parameters
    ----------
    url : str
        Service origin without a trailing slash.
    label : str
        Human-readable service name used in the skip reason.
    """
    try:
        async with pinned_async_client(JARVIS_SERVICE_POLICY, timeout=5.0) as probe:
            response = await probe.get(f"{url}/health")
    except (httpx.HTTPError, RuntimeError) as exc:
        pytest.skip(f"{label} at {url} is not reachable: {type(exc).__name__}")
    if response.status_code != 200:
        pytest.skip(f"{label} at {url} answered /health with {response.status_code}")


async def _require_assertion(
    platform: httpx.AsyncClient,
    config: BotConfig,
    user_id: int,
) -> None:
    """Skip unless Platform will mint a downstream assertion for *user_id*.

    Platform answers 404 when the user has no active Telegram pairing and 403
    when the service credential or capability is refused. Either way the live
    path cannot run, and the precise status belongs in the skip reason rather
    than in five identical handler failures.

    Parameters
    ----------
    platform : httpx.AsyncClient
        Client carrying Telegram's Platform service credential.
    config : BotConfig
        Configuration naming the Platform origin.
    user_id : int
        Candidate paired JARVIS user.
    """
    # TelegramBackendAuth posts this exact body to the authorize route, so a
    # probe that succeeds here proves the credential mints real assertions.
    try:
        response = await platform.post(
            f"{config.platform_api_url}/internal/telegram/authorize",
            json={
                "audience": "research",
                "method": "GET",
                "path": "/api/papers/feed",
                "request_id": "live-backend-harness-probe",
                "user_id": user_id,
            },
        )
    except (httpx.HTTPError, RuntimeError) as exc:
        pytest.skip(f"Platform at {config.platform_api_url} is not reachable: {type(exc).__name__}")
    if response.status_code != 200:
        pytest.skip(
            f"Platform refused an assertion for user {user_id} "
            f"(status {response.status_code}); pair a chat in the running deployment "
            "or set JARVIS_LIVE_BACKEND_USER_ID"
        )


@pytest.fixture
async def live_backend() -> AsyncIterator[LiveBackend]:
    """Yield real Platform and downstream clients, or skip with a precise reason.

    Yields
    ------
    LiveBackend
        Configuration, authenticated downstream client, and paired user id.
    """
    config = _live_bot_config()
    user_id = int(os.environ.get("JARVIS_LIVE_BACKEND_USER_ID", "1"))

    await _require_healthy(config.paper_ingestion_url, "Research service")
    await _require_healthy(config.learning_engine_url, "Learning service")

    # Reproduces the two clients the bot builds at startup: a Platform service
    # client, and a downstream client that carries TelegramBackendAuth.
    async with pinned_async_client(
        JARVIS_SERVICE_POLICY,
        timeout=10.0,
        headers=service_headers(config),
    ) as platform_client:
        await _require_assertion(platform_client, config, user_id)
        async with pinned_async_client(
            JARVIS_SERVICE_POLICY,
            timeout=120.0,
            auth=TelegramBackendAuth(config, platform_client),
        ) as http_client:
            yield LiveBackend(config=config, http=http_client, user_id=user_id)


@pytest.fixture(autouse=True)
def _live_auth_patch(live_backend: LiveBackend) -> Iterator[None]:
    """Resolve the synthesized chat to the configured paired user.

    Only the bot-side authorization decision is replaced. Every HTTP call the
    handler then makes still travels to the real backend with real credentials.
    """
    # auth_required awaits auth_check and unpacks (authorized, jarvis_user_id).
    with patch(
        "telegram_bot.handlers.commands._auth.auth_check",
        new_callable=AsyncMock,
        return_value=(True, live_backend.user_id),
    ):
        yield


def _live_update_and_context(
    backend: LiveBackend,
    args: list[str] | None = None,
) -> tuple[MagicMock, MagicMock]:
    """Build a synthesized Update plus a context bound to the live client.

    Parameters
    ----------
    backend : LiveBackend
        Live wiring produced by the ``live_backend`` fixture.
    args : list of str, optional
        Command arguments, as python-telegram-bot would supply them.

    Returns
    -------
    tuple of MagicMock
        The Update whose ``message.reply_text`` records replies, and the
        callback context handlers read their clients from.
    """
    update = MagicMock()
    update.effective_chat = MagicMock()
    update.effective_chat.id = _LIVE_CHAT_ID
    update.effective_chat.type = "private"
    update.message = MagicMock()
    update.message.reply_text = AsyncMock()

    context = make_ptb_context(
        MagicMock(),
        backend.config,
        options=PTBContextOptions(
            http_client=backend.http,
            args=list(args or []),
            user_data={"jarvis_user_id": backend.user_id},
        ),
    )
    return update, context


def _replies(update: MagicMock) -> list[str]:
    """Return the text of every reply the handler sent.

    Parameters
    ----------
    update : MagicMock
        Update whose ``message.reply_text`` recorded the calls.

    Returns
    -------
    list of str
        Reply texts in send order.
    """
    return [call.args[0] for call in update.message.reply_text.await_args_list]


def _assert_no_raw_error(text: str) -> None:
    """Assert a reply carries no exception or traceback text.

    Parameters
    ----------
    text : str
        Reply text produced by a handler.
    """
    for leak in ("Traceback", "Exception", "httpx.", "HTTPStatusError", "<class "):
        assert leak not in text, f"reply leaked internal error text: {leak!r} in {text!r}"


def _assert_keyboard_rows(markup: Any, expected_prefixes: tuple[str, ...]) -> None:
    """Assert an inline keyboard is well formed and carries the expected actions.

    Parameters
    ----------
    markup : Any
        ``InlineKeyboardMarkup`` attached to a reply.
    expected_prefixes : tuple of str
        Callback-data prefixes every row must offer, in order.
    """
    rows = markup.inline_keyboard
    assert rows, "keyboard has no rows"
    for row in rows:
        assert row, "keyboard row is empty"
        for button in row:
            assert button.text, "keyboard button has no label"
            assert button.callback_data, "keyboard button has no callback data"
    flat = [button.callback_data for row in rows for button in row]
    for prefix in expected_prefixes:
        assert any(data.startswith(prefix) for data in flat), (
            f"keyboard is missing a {prefix!r} action; got {flat!r}"
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_live_papers_command_lists_library_shape(live_backend: LiveBackend) -> None:
    """``/papers <query>`` renders real library rows or the real empty-search reply.

    A dead feed contract surfaces here as the handler's own failure reply,
    because ``_feed_papers`` rejects any envelope that is not ``{"papers": [...]}``.
    """
    # With arguments papers_command searches the feed, and _feed_papers raises
    # unless the response is the documented envelope.
    update, context = _live_update_and_context(live_backend, args=["learning"])

    await papers_command(update, context)

    replies = _replies(update)
    assert replies, "/papers sent no reply"
    for text in replies:
        _assert_no_raw_error(text)
        assert "Failed to load library" not in text, (
            "the live library feed did not answer the shape the handler parses"
        )

    if len(replies) == 1 and (
        "No library papers match" in replies[0] or "Your Library is empty" in replies[0]
    ):
        return

    # The first reply is the stage header naming the view; the cards follow it.
    assert "Library search" in replies[0], "/papers must open with its stage header"
    assert len(replies) <= 11, "/papers must cap the live feed at ten cards after its header"
    for call in update.message.reply_text.await_args_list[1:]:
        assert "📄" in call.args[0], "a library card must carry the paper header marker"
        # _library_keyboard renders one row of star / trash / detail actions.
        _assert_keyboard_rows(
            call.kwargs["reply_markup"],
            ("paper:star:", "paper:trash:", "paper_detail_"),
        )


async def test_live_discover_command_reports_a_real_search(live_backend: LiveBackend) -> None:
    """``/discover <query>`` reports a live multi-source search or a clean degraded state."""
    # discover_command searches, then formats: "Found ..." when sources
    # answered, otherwise the no-source line.
    update, context = _live_update_and_context(live_backend, args=["protein", "folding"])

    await discover_command(update, context)

    update.message.reply_text.assert_awaited_once()
    text = _replies(update)[0]
    _assert_no_raw_error(text)
    assert "Discovery failed" not in text, (
        "the live discovery endpoint did not answer the shape the handler parses"
    )
    assert text.startswith("Found ") or text.startswith("No external source returned a paper"), (
        f"discovery reply matched no live shape: {text!r}"
    )


async def test_live_stats_command_renders_every_stat_line(live_backend: LiveBackend) -> None:
    """``/stats`` renders all five learning-stat lines from the live payload."""
    # format_review_stats reads total_cards, due_now, reviewed_today,
    # average_retention and streak_days; all five lines must render.
    update, context = _live_update_and_context(live_backend)

    await stats_command(update, context)

    update.message.reply_text.assert_awaited_once()
    text = _replies(update)[0]
    _assert_no_raw_error(text)
    assert "Failed to retrieve learning stats" not in text, (
        "the live stats endpoint did not answer the shape the handler parses"
    )
    for line in (
        "<b>Learning Stats</b>",
        "Total cards:",
        "Due now:",
        "Reviewed today:",
        "Retention rate:",
        "Streak:",
    ):
        assert line in text, f"stats reply is missing {line!r}"


async def test_live_briefing_command_composes_without_degrading(
    live_backend: LiveBackend,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``/briefing`` renders every section and degrades none of its gathers.

    ``briefing_command`` swallows a failed section and renders a zero, so the
    reply text alone cannot distinguish a live backend from a dead one. The
    handler's own error logs are therefore part of the assertion.
    """
    # briefing_command logs "Failed to fetch ..." and continues per section, so
    # a silently degraded section still renders a briefing: assert on the logs too.
    update, context = _live_update_and_context(live_backend)

    with caplog.at_level(logging.ERROR, logger="telegram_bot.handlers.commands.paper_commands"):
        await briefing_command(update, context)

    update.message.reply_text.assert_awaited_once()
    text = _replies(update)[0]
    _assert_no_raw_error(text)
    assert "<b>Morning Briefing</b>" in text
    assert "papers added to your library since midnight UTC" in text
    assert "waiting in your inbox" in text
    assert "cards due for review right now" in text

    degraded = [record.message for record in caplog.records if "Failed to fetch" in record.message]
    assert not degraded, f"live briefing sections degraded: {degraded}"


async def test_live_tasks_command_lists_task_shape(live_backend: LiveBackend) -> None:
    """``/tasks`` renders the in-progress list header or the real empty reply."""
    # tasks_command lists in-progress tasks as a bulleted list.
    update, context = _live_update_and_context(live_backend)

    await tasks_command(update, context)

    update.message.reply_text.assert_awaited_once()
    text = _replies(update)[0]
    _assert_no_raw_error(text)
    assert "Couldn't reach JARVIS" not in text, (
        "the live task endpoint did not answer the shape the handler parses"
    )

    if text == "No in-progress tasks.":
        return
    assert "<b>In-Progress Tasks</b>" in text
    bullets = [line for line in text.splitlines() if line.startswith("• [")]
    assert bullets, f"task list carries a header but no rows: {text!r}"
    for line in bullets:
        assert "]" in line, f"task row is missing its identifier bracket: {line!r}"


async def test_live_next_command_advances_past_acted_cards(live_backend: LiveBackend) -> None:
    """``/next`` renders a live Pulse card, the finished-deck reply, or an empty state.

    The card it picks is the highest-ranked one whose backend lifecycle state
    says the user has not acted on it, so a deck read that stopped reporting
    that state surfaces here as the finished-deck reply on a deck that still
    has unread cards.
    """
    update, context = _live_update_and_context(live_backend)

    await next_command(update, context)

    update.message.reply_text.assert_awaited_once()
    text = _replies(update)[0]
    _assert_no_raw_error(text)
    assert "Failed to load next recommendation" not in text, (
        "the live deck endpoint did not answer the shape the handler parses"
    )

    empty_states = ("No Pulse deck yet", "No Pulse cards are available", "no papers are available")
    if any(state in text for state in empty_states) or "acted on all" in text:
        return

    assert "Pulse for" in text or "Earlier Pulse from" in text, (
        f"a live card reply must carry the deck status header: {text!r}"
    )
    _assert_keyboard_rows(
        update.message.reply_text.await_args.kwargs["reply_markup"],
        ("paper:feedback_pos:", "paper:feedback_neg:", "paper:save:"),
    )


async def test_live_pulse_now_command_waits_for_the_job_it_started(
    live_backend: LiveBackend,
) -> None:
    """``/pulse_now`` enqueues a real run, follows it, and answers with an outcome.

    Skipped unless the generate opt-in is set as well: this is the only handler
    in the module that changes deployment state.
    """
    if os.environ.get(_GENERATE_OPT_IN_ENV) != "1":
        pytest.skip(f"starting a real Pulse run requires {_GENERATE_OPT_IN_ENV}=1")

    update, context = _live_update_and_context(live_backend)

    await pulse_now_command(update, context)

    replies = _replies(update)
    assert replies, "/pulse_now sent no reply"
    for text in replies:
        _assert_no_raw_error(text)
    assert "Failed to trigger Pulse generation" not in replies[0], (
        "the live generate endpoint did not answer the shape the handler parses"
    )
    assert "Pulse generation started" in replies[0]
    # The handler either delivered the deck through the scheduled path (which
    # sends through the bot, not reply_text) or answered with one of its two
    # honest outcomes.
    if len(replies) > 1:
        assert "still generating" in replies[1] or "did not finish" in replies[1], (
            f"the wait must end in a stated outcome: {replies[1]!r}"
        )
