"""Bot-behavior tests for the author-alerts orchestrator.

The bot no longer matches authors or writes the dedupe log itself; that logic
(plus per-user ``last_checked_at``) now lives behind the Paper Ingestion
service.  ``run_author_alerts`` simply calls
``services_client.check_authors`` once per paired user and renders one Telegram
message per ``match`` in the response.

These tests assert that delegated behavior at the http_client boundary.  (The
SQL-shape invariant for ``db_helpers.record_author_alert`` — formerly checked
here — is now owned by the Paper Ingestion / jarvis_common test suite that
actually calls it.)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from jarvis_common.testing import make_bot_config
from jarvis_common.testing_telegram import make_http_response
from pydantic import SecretStr
from telegram_bot.config import BotConfig
from telegram_bot.orchestration import author_alerts as author_alerts_mod
from telegram_bot.platform_client import UserPairing


def _check_then_ack(matches: list[dict], *, check_status: int = 200):
    """Route the bot's POSTs by URL: the check answers with *matches*, acks 200.

    The bot now issues two kinds of POST, so a flat response list would hand an
    acknowledgement the next pairing's check payload.
    """

    async def _post(url, *_args, **_kwargs):
        if url.endswith("/api/authors/check"):
            return make_http_response(
                {
                    "matches": matches,
                    "new_papers": sum(len(match["papers"]) for match in matches),
                    "authors_checked": len(matches),
                },
                status=check_status,
            )
        assert url.endswith("/api/authors/alerts/ack"), f"unexpected POST to {url}"
        return make_http_response({"recorded": 1})

    return _post


def _ack_payloads(http_client) -> list[dict]:
    """The bodies of every delivery acknowledgement the bot sent, in order."""
    return [
        call.kwargs["json"]
        for call in http_client.post.await_args_list
        if call.args[0].endswith("/api/authors/alerts/ack")
    ]


@pytest.mark.asyncio
async def test_run_author_alerts_calls_check_authors_per_pairing() -> None:
    """Each paired user gets exactly one owner-scoped POST /api/authors/check."""
    bot = AsyncMock()
    pool = AsyncMock()
    config = make_bot_config(BotConfig, jarvis_api_key=SecretStr("secret"))

    http_client = AsyncMock(spec=httpx.AsyncClient)
    http_client.post.return_value = make_http_response(
        {"matches": [], "new_papers": 0, "authors_checked": 0}
    )

    with patch(
        "telegram_bot.orchestration.author_alerts.list_user_pairings",
        AsyncMock(
            return_value=[
                UserPairing(user_id=1, chat_id=100),
                UserPairing(user_id=2, chat_id=200),
            ]
        ),
    ):
        await author_alerts_mod.run_author_alerts(http_client, pool, bot, config)

    assert http_client.post.await_count == 2
    seen_owner_ids = set()
    for call in http_client.post.await_args_list:
        url = call.args[0]
        assert url.endswith("/api/authors/check")
        assert "X-API-Key" not in call.kwargs["headers"]
        seen_owner_ids.add(call.kwargs["headers"]["X-Jarvis-Paired-User-Id"])
        # The bot delivers each match itself, so it asks for them unrecorded.
        assert call.kwargs["json"] == {"acknowledges_delivery": True}
    assert seen_owner_ids == {"1", "2"}
    # No matches → nothing sent.
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_author_alerts_renders_one_message_per_match() -> None:
    """Each match in the service response becomes one HTML alert to that chat."""
    bot = AsyncMock()
    pool = AsyncMock()
    config = make_bot_config(BotConfig, jarvis_api_key=SecretStr("secret"))

    http_client = AsyncMock(spec=httpx.AsyncClient)
    http_client.post.side_effect = _check_then_ack(
        [
            {
                "tracked_author_id": 11,
                "author_name": "Alice Smith",
                "papers": [{"id": 1, "title": "Paper A", "url": "https://e.x/a"}],
            },
            {
                "tracked_author_id": 12,
                "author_name": "Bob Jones",
                "papers": [{"id": 2, "title": "Paper B", "url": "https://e.x/b"}],
            },
        ]
    )

    with patch(
        "telegram_bot.orchestration.author_alerts.list_user_pairings",
        AsyncMock(return_value=[UserPairing(user_id=7, chat_id=555)]),
    ):
        await author_alerts_mod.run_author_alerts(http_client, pool, bot, config)

    assert bot.send_message.await_count == 2
    sent_texts = []
    for call in bot.send_message.await_args_list:
        assert call.kwargs["chat_id"] == 555
        assert call.kwargs["parse_mode"] == "HTML"
        sent_texts.append(call.kwargs["text"])
    joined = "\n".join(sent_texts)
    assert "Alice Smith" in joined
    assert "Bob Jones" in joined
    # Each delivered alert is acknowledged, so it is not offered a second time.
    assert _ack_payloads(http_client) == [
        {"tracked_author_id": 11, "paper_ids": [1]},
        {"tracked_author_id": 12, "paper_ids": [2]},
    ]


@pytest.mark.asyncio
async def test_run_author_alerts_leaves_an_undelivered_alert_unacknowledged() -> None:
    """A failed send is never acknowledged, so the alert stays on offer.

    The service records an alert only when the bot acknowledges it, so the
    absence of the acknowledgement is what carries the undelivered alert into
    the next check. A later match in the same response is still delivered.
    """
    bot = AsyncMock()
    pool = AsyncMock()
    config = make_bot_config(BotConfig, jarvis_api_key=SecretStr("secret"))

    bot.send_message.side_effect = [RuntimeError("telegram is unreachable"), None]

    http_client = AsyncMock(spec=httpx.AsyncClient)
    http_client.post.side_effect = _check_then_ack(
        [
            {
                "tracked_author_id": 11,
                "author_name": "Alice Smith",
                "papers": [{"id": 1, "title": "Paper A", "url": "https://e.x/a"}],
            },
            {
                "tracked_author_id": 12,
                "author_name": "Bob Jones",
                "papers": [{"id": 2, "title": "Paper B", "url": "https://e.x/b"}],
            },
        ]
    )

    with patch(
        "telegram_bot.orchestration.author_alerts.list_user_pairings",
        AsyncMock(return_value=[UserPairing(user_id=7, chat_id=555)]),
    ):
        await author_alerts_mod.run_author_alerts(http_client, pool, bot, config)

    assert bot.send_message.await_count == 2
    # Only the delivered alert is acknowledged; Alice's survives for the next run.
    assert _ack_payloads(http_client) == [{"tracked_author_id": 12, "paper_ids": [2]}]


@pytest.mark.asyncio
async def test_run_author_alerts_one_pairing_error_does_not_abort_others() -> None:
    """A 5xx on one pairing's check is logged-and-skipped; later pairings still run."""
    bot = AsyncMock()
    pool = AsyncMock()
    config = make_bot_config(BotConfig, jarvis_api_key=SecretStr("secret"))

    healthy = _check_then_ack(
        [
            {
                "tracked_author_id": 13,
                "author_name": "Carol",
                "papers": [{"id": 3, "title": "Paper C", "url": "https://e.x/c"}],
            }
        ]
    )

    async def _post(url, *args, **kwargs):
        # First pairing's check errors, second succeeds.
        if kwargs["headers"]["X-Jarvis-Paired-User-Id"] == "1":
            return make_http_response({"detail": "boom"}, status=503)
        return await healthy(url, *args, **kwargs)

    http_client = AsyncMock(spec=httpx.AsyncClient)
    http_client.post.side_effect = _post

    with patch(
        "telegram_bot.orchestration.author_alerts.list_user_pairings",
        AsyncMock(
            return_value=[
                UserPairing(user_id=1, chat_id=100),
                UserPairing(user_id=2, chat_id=200),
            ]
        ),
    ):
        await author_alerts_mod.run_author_alerts(http_client, pool, bot, config)

    # Both pairings attempted; only the healthy one delivered and acknowledged.
    checks = [call for call in http_client.post.await_args_list if call.args[0].endswith("/check")]
    assert len(checks) == 2
    bot.send_message.assert_awaited_once()
    _, kwargs = bot.send_message.await_args
    assert kwargs["chat_id"] == 200
    assert "Carol" in kwargs["text"]
    assert _ack_payloads(http_client) == [{"tracked_author_id": 13, "paper_ids": [3]}]
