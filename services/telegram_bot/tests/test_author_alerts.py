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
        seen_owner_ids.add(call.kwargs["headers"]["X-Owner-User-Id"])
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
    http_client.post.return_value = make_http_response(
        {
            "matches": [
                {
                    "author_name": "Alice Smith",
                    "papers": [{"id": 1, "title": "Paper A", "url": "https://e.x/a"}],
                },
                {
                    "author_name": "Bob Jones",
                    "papers": [{"id": 2, "title": "Paper B", "url": "https://e.x/b"}],
                },
            ],
            "new_papers": 2,
            "authors_checked": 2,
        }
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


@pytest.mark.asyncio
async def test_run_author_alerts_one_pairing_error_does_not_abort_others() -> None:
    """A 5xx on one pairing's check is logged-and-skipped; later pairings still run."""
    bot = AsyncMock()
    pool = AsyncMock()
    config = make_bot_config(BotConfig, jarvis_api_key=SecretStr("secret"))

    ok_resp = make_http_response(
        {
            "matches": [
                {
                    "author_name": "Carol",
                    "papers": [{"id": 3, "title": "Paper C", "url": "https://e.x/c"}],
                }
            ],
            "new_papers": 1,
            "authors_checked": 1,
        }
    )
    err_resp = make_http_response({"detail": "boom"}, status=503)

    http_client = AsyncMock(spec=httpx.AsyncClient)
    # First pairing errors, second succeeds.
    http_client.post.side_effect = [err_resp, ok_resp]

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

    # Both pairings attempted; only the healthy one delivered.
    assert http_client.post.await_count == 2
    bot.send_message.assert_awaited_once()
    _, kwargs = bot.send_message.await_args
    assert kwargs["chat_id"] == 200
    assert "Carol" in kwargs["text"]
