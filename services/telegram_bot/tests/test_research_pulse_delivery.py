"""Tests for the thin Pulse delivery orchestrator.

Covers the post-Stream-K rewrite of ``research_pulse``: instead of the old
~165-line reimplementation of the Pulse pipeline, the orchestrator is now a
thin delivery layer that:

1. Fetches ``GET /api/pulse/today`` from paper_ingestion.
2. Formats the top N cards with inline Up/Down/Save buttons.
3. Sends one message per card, with graceful-degradation fallbacks on errors.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from telegram_bot.config import BotConfig
from telegram_bot.orchestration import research_pulse


def _make_config(api_key: str = "secret") -> BotConfig:
    return BotConfig(
        telegram_token="token",
        telegram_chat_id=1234,
        database_url="postgres://example",
        paper_ingestion_url="http://paper-ingestion:8000",
        learning_engine_url="http://learning-engine:8001",
        jarvis_api_key=api_key,
    )


def _make_deck(num_cards: int = 3) -> dict:
    """Build a fake PulseDeckResponse JSON payload with ``num_cards`` cards."""
    return {
        "deck_id": 7,
        "deck_date": "2026-04-11",
        "card_count": num_cards,
        "generated_at": "2026-04-11T06:00:00+00:00",
        "cards": [
            {
                "card_id": 100 + i,
                "paper_id": 40 + i,
                "paper_title": f"Paper {i}",
                "paper_authors": [f"Author {i}"],
                "paper_url": f"http://example.com/{i}",
                "rank": i + 1,
                "score": 0.9 - 0.1 * i,
                "llm_relevance": 8,
                "llm_novelty": 7,
                "reasoning": f"Because reason {i}",
                "signals": {"recency": 0.5},
            }
            for i in range(num_cards)
        ],
        "stats": {},
    }


def _ok_response(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = payload
    return resp


@pytest.mark.asyncio
async def test_fetches_pulse_today_and_sends_cards():
    """Deck with 3 cards → 3 per-card messages + 1 header."""
    bot = AsyncMock()
    http_client = AsyncMock(spec=httpx.AsyncClient)
    http_client.get.return_value = _ok_response(_make_deck(3))
    db_pool = AsyncMock()

    await research_pulse.run_research_pulse(http_client, db_pool, bot, _make_config())

    # One GET to /api/pulse/today with auth header
    http_client.get.assert_awaited_once()
    _, kwargs = http_client.get.await_args
    assert "/api/pulse/today" in http_client.get.await_args[0][0]
    assert kwargs["headers"]["X-API-Key"] == "secret"

    # Three per-card messages (optional header message is allowed but not
    # required; we only assert that every card produced at least one send).
    assert bot.send_message.await_count >= 3
    sent_texts = [call.kwargs.get("text", "") for call in bot.send_message.await_args_list]
    joined = "\n".join(sent_texts)
    for i in range(3):
        assert f"Paper {i}" in joined


@pytest.mark.asyncio
async def test_empty_deck_sends_fallback_message():
    """``/api/pulse/today`` 404 → friendly fallback, no crashes."""
    bot = AsyncMock()
    http_client = AsyncMock(spec=httpx.AsyncClient)
    http_client.get.side_effect = httpx.HTTPStatusError(
        "404",
        request=MagicMock(),
        response=MagicMock(status_code=404),
    )
    db_pool = AsyncMock()

    await research_pulse.run_research_pulse(http_client, db_pool, bot, _make_config())

    bot.send_message.assert_awaited()
    sent_text = bot.send_message.await_args.kwargs["text"]
    assert "/pulse_now" in sent_text or "No Pulse" in sent_text


@pytest.mark.asyncio
async def test_api_failure_sends_diagnostic(caplog):
    """Unexpected HTTP error → diagnostic message + logged warning."""
    bot = AsyncMock()
    http_client = AsyncMock(spec=httpx.AsyncClient)
    http_client.get.side_effect = httpx.ConnectError("offline")
    db_pool = AsyncMock()

    with caplog.at_level("WARNING"):
        await research_pulse.run_research_pulse(http_client, db_pool, bot, _make_config())

    bot.send_message.assert_awaited()
    # Must not raise; the scheduler wraps but a thin delivery layer should
    # degrade gracefully on its own.


@pytest.mark.asyncio
async def test_card_message_has_three_inline_buttons(monkeypatch):
    """Each per-card send must carry up/down/save buttons with the right
    ``callback_data`` values."""
    captured_keyboards: list = []

    def fake_markup(rows):
        # Capture the nested rows of buttons so the test can inspect them.
        captured_keyboards.append(rows)
        return MagicMock(_rows=rows)

    monkeypatch.setattr(research_pulse, "InlineKeyboardMarkup", fake_markup)
    monkeypatch.setattr(
        research_pulse,
        "InlineKeyboardButton",
        lambda text, callback_data: {"text": text, "callback_data": callback_data},
    )

    bot = AsyncMock()
    http_client = AsyncMock(spec=httpx.AsyncClient)
    http_client.get.return_value = _ok_response(_make_deck(2))
    db_pool = AsyncMock()

    await research_pulse.run_research_pulse(http_client, db_pool, bot, _make_config())

    # Exactly one keyboard per card (2 cards → 2 keyboards)
    assert len(captured_keyboards) == 2
    for rows in captured_keyboards:
        # Flatten rows into a single button list
        buttons = [b for row in rows for b in row]
        assert len(buttons) == 3
        callback_values = sorted(b["callback_data"].split("_")[1] for b in buttons)
        assert callback_values == ["down", "save", "up"]

    # First card's paper_id is 40 — all three buttons should encode it.
    first_card_buttons = [b for row in captured_keyboards[0] for b in row]
    for button in first_card_buttons:
        assert button["callback_data"].endswith("_40")


@pytest.mark.asyncio
async def test_deck_is_capped_to_top_n():
    """A 20-card deck should only send up to PULSE_TELEGRAM_TOP_N cards."""
    bot = AsyncMock()
    http_client = AsyncMock(spec=httpx.AsyncClient)
    http_client.get.return_value = _ok_response(_make_deck(20))
    db_pool = AsyncMock()

    await research_pulse.run_research_pulse(http_client, db_pool, bot, _make_config())

    # PULSE_TELEGRAM_TOP_N is 5 (brevity). With optional header, up to 6 sends.
    assert bot.send_message.await_count <= research_pulse.PULSE_TELEGRAM_TOP_N + 1
    assert bot.send_message.await_count >= research_pulse.PULSE_TELEGRAM_TOP_N
