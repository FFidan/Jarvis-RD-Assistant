"""Unit tests for the weekly paper digest workflow."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from telegram_bot.config import BotConfig
from telegram_bot.orchestration import paper_digest


def _make_config(api_key: str = "secret") -> BotConfig:
    """Create a minimal bot config for digest tests."""
    return BotConfig(
        telegram_token="token",
        telegram_chat_id=1234,
        database_url="postgres://example",
        paper_ingestion_url="http://paper-ingestion:8000",
        learning_engine_url="http://learning-engine:8001",
        jarvis_api_key=api_key,
    )


@pytest.mark.asyncio
async def test_fetch_digest_from_api_returns_payload_and_auth_header():
    """The digest fetch helper returns parsed JSON and includes the API key header."""
    http_client = AsyncMock(spec=httpx.AsyncClient)
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"topics": [{"name": "LLMs"}]}
    http_client.get.return_value = response

    result = await paper_digest._fetch_digest_from_api(http_client, _make_config())

    assert result == {"topics": [{"name": "LLMs"}]}
    _, kwargs = http_client.get.await_args
    assert kwargs["headers"]["X-API-Key"] == "secret"


@pytest.mark.asyncio
async def test_fetch_digest_from_api_returns_none_on_error():
    """HTTP failures degrade to None so the caller can fall back."""
    http_client = AsyncMock(spec=httpx.AsyncClient)
    http_client.get.side_effect = httpx.ConnectError("offline")

    result = await paper_digest._fetch_digest_from_api(http_client, _make_config())

    assert result is None


@pytest.mark.asyncio
async def test_fetch_digest_from_api_omits_auth_header_without_api_key():
    """Empty bot API keys should not emit an auth header."""
    http_client = AsyncMock(spec=httpx.AsyncClient)
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"topics": []}
    http_client.get.return_value = response

    result = await paper_digest._fetch_digest_from_api(http_client, _make_config(api_key=""))

    assert result == {"topics": []}
    _, kwargs = http_client.get.await_args
    assert kwargs["headers"] == {}


@pytest.mark.asyncio
async def test_send_chunked_sends_single_message_when_short():
    """Short digests are sent as a single Telegram message."""
    bot = AsyncMock()

    await paper_digest._send_chunked(bot, 1234, ["line one", "line two"])

    bot.send_message.assert_awaited_once()
    _, kwargs = bot.send_message.await_args
    assert kwargs["chat_id"] == 1234
    assert kwargs["parse_mode"] == "HTML"


@pytest.mark.asyncio
async def test_send_chunked_splits_long_messages():
    """Long digests are split into multiple Telegram-sized chunks."""
    bot = AsyncMock()
    lines = [f"line-{i}-{'x' * 1000}" for i in range(5)]

    await paper_digest._send_chunked(bot, 1234, lines)

    assert bot.send_message.await_count >= 2
    sent_text = "\n".join(call.kwargs["text"] for call in bot.send_message.await_args_list)
    assert "line-0-" in sent_text
    assert "line-4-" in sent_text


@pytest.mark.asyncio
async def test_run_paper_digest_uses_llm_digest_when_topics_present():
    """run_paper_digest prefers the API digest when it returns topics."""
    bot = AsyncMock()
    http_client = AsyncMock(spec=httpx.AsyncClient)
    db_pool = AsyncMock()
    config = _make_config()

    with (
        patch.object(
            paper_digest,
            "_fetch_digest_from_api",
            AsyncMock(return_value={"topics": [{"name": "Agents"}], "total_papers": 2}),
        ) as fetch_digest,
        patch.object(paper_digest, "format_weekly_digest", return_value="digest line"),
        patch.object(paper_digest, "_send_chunked", AsyncMock()) as send_chunked,
        patch.object(paper_digest, "_simple_digest", AsyncMock()) as simple_digest,
    ):
        await paper_digest.run_paper_digest(http_client, db_pool, bot, config)

    fetch_digest.assert_awaited_once()
    send_chunked.assert_awaited_once()
    simple_digest.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_paper_digest_falls_back_to_simple_digest():
    """run_paper_digest falls back when the API returns no topic data."""
    bot = AsyncMock()
    http_client = AsyncMock(spec=httpx.AsyncClient)
    db_pool = AsyncMock()
    config = _make_config()

    with (
        patch.object(paper_digest, "_fetch_digest_from_api", AsyncMock(return_value=None)),
        patch.object(paper_digest, "_simple_digest", AsyncMock()) as simple_digest,
    ):
        await paper_digest.run_paper_digest(http_client, db_pool, bot, config)

    simple_digest.assert_awaited_once_with(db_pool, bot, config, 1234)
