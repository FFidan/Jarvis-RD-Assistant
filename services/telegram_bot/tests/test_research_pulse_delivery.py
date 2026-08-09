"""Tests for the thin Pulse delivery orchestrator.

Covers the post-Stream-K rewrite of ``research_pulse``: instead of the old
~165-line reimplementation of the Pulse pipeline, the orchestrator is now a
thin delivery layer that:

1. Fetches ``GET /api/pulse/today`` from paper_ingestion.
2. Formats the top N cards with inline Up/Down/Save buttons.
3. Sends one message per card, with graceful-degradation fallbacks on errors.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from jarvis_common.testing import make_bot_config
from pydantic import SecretStr
from telegram_bot.config import BotConfig
from telegram_bot.formatters import format_pulse_card
from telegram_bot.orchestration import research_pulse
from telegram_bot.owner import UserPairing

_DEFAULT_PAIRING = [UserPairing(user_id=1, chat_id=1234)]


@pytest.mark.parametrize(
    ("verified", "confidence", "expected"),
    [
        (False, "UNVERIFIED", "Evidence check: Unverified"),
        (True, "HIGH", "Evidence confidence: High"),
        (True, None, "Evidence check: Verified"),
        (None, "MEDIUM", "Evidence confidence: Medium"),
        (None, None, "Evidence check: Not reported"),
    ],
)
def test_pulse_card_exposes_each_evidence_state(
    verified: bool | None,
    confidence: str | None,
    expected: str,
) -> None:
    text = format_pulse_card(
        {
            "paper_title": "A paper",
            "paper_authors": [],
            "score": 0.8,
            "rank": 1,
            "reasoning_verified": verified,
            "reasoning_confidence": confidence,
        }
    )

    assert expected in text


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
        "degraded_reason": None,
        "is_stale": False,
        "stale_age_days": None,
        "empty_reason": None,
    }


def _ok_response(payload: dict | None) -> MagicMock:
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

    with patch("telegram_bot.owner.list_user_pairings", AsyncMock(return_value=_DEFAULT_PAIRING)):
        await research_pulse.run_research_pulse(
            http_client,
            db_pool,
            bot,
            make_bot_config(BotConfig, jarvis_api_key=SecretStr("secret")),
        )

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
    assert "Current Pulse for April 11" in joined


@pytest.mark.asyncio
async def test_stale_degraded_deck_is_labelled_without_internal_diagnostic():
    deck = _make_deck(1)
    deck.update(
        {
            "is_stale": True,
            "stale_age_days": 2,
            "degraded_reason": "database password rejected on internal-host",
        }
    )
    deck["cards"][0]["reasoning_verified"] = False
    deck["cards"][0]["reasoning_confidence"] = "UNVERIFIED"
    bot = AsyncMock()
    http_client = AsyncMock(spec=httpx.AsyncClient)
    http_client.get.return_value = _ok_response(deck)

    with patch("telegram_bot.owner.list_user_pairings", AsyncMock(return_value=_DEFAULT_PAIRING)):
        await research_pulse.run_research_pulse(
            http_client,
            AsyncMock(),
            bot,
            make_bot_config(BotConfig, jarvis_api_key=SecretStr("secret")),
        )

    joined = "\n".join(call.kwargs["text"] for call in bot.send_message.await_args_list)
    assert "Earlier Pulse from April 11 (2 days old)" in joined
    assert "reduced signals" in joined
    assert "Unverified" in joined
    assert "database password" not in joined
    assert "internal-host" not in joined


@pytest.mark.asyncio
async def test_empty_current_deck_explains_that_no_papers_are_available_yet():
    deck = _make_deck(0)
    deck["empty_reason"] = "no_data_yet"
    bot = AsyncMock()
    http_client = AsyncMock(spec=httpx.AsyncClient)
    http_client.get.return_value = _ok_response(deck)

    with patch("telegram_bot.owner.list_user_pairings", AsyncMock(return_value=_DEFAULT_PAIRING)):
        await research_pulse.run_research_pulse(
            http_client,
            AsyncMock(),
            bot,
            make_bot_config(BotConfig, jarvis_api_key=SecretStr("secret")),
        )

    text = bot.send_message.await_args.kwargs["text"]
    assert "no papers are available" in text.lower()
    assert "April 11" in text


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

    with patch("telegram_bot.owner.list_user_pairings", AsyncMock(return_value=_DEFAULT_PAIRING)):
        await research_pulse.run_research_pulse(
            http_client,
            db_pool,
            bot,
            make_bot_config(BotConfig, jarvis_api_key=SecretStr("secret")),
        )

    bot.send_message.assert_awaited()
    sent_text = bot.send_message.await_args.kwargs["text"]
    assert "/pulse_now" in sent_text or "No Pulse" in sent_text


@pytest.mark.asyncio
async def test_null_body_sends_fallback_message():
    """``/api/pulse/today`` now returns 200 + JSON null (not 404) for an empty
    deck. The orchestrator must coalesce the null body to an empty deck and send
    the friendly fallback rather than crashing on ``None.cards``.
    """
    bot = AsyncMock()
    http_client = AsyncMock(spec=httpx.AsyncClient)
    http_client.get.return_value = _ok_response(None)  # 200 + JSON null
    db_pool = AsyncMock()

    with patch("telegram_bot.owner.list_user_pairings", AsyncMock(return_value=_DEFAULT_PAIRING)):
        await research_pulse.run_research_pulse(
            http_client,
            db_pool,
            bot,
            make_bot_config(BotConfig, jarvis_api_key=SecretStr("secret")),
        )

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

    with (
        caplog.at_level("WARNING"),
        patch("telegram_bot.owner.list_user_pairings", AsyncMock(return_value=_DEFAULT_PAIRING)),
    ):
        await research_pulse.run_research_pulse(
            http_client,
            db_pool,
            bot,
            make_bot_config(BotConfig, jarvis_api_key=SecretStr("secret")),
        )

    bot.send_message.assert_awaited()
    sent_text = bot.send_message.await_args.kwargs["text"]
    # The diagnostic message must mention the failure so the user knows what went wrong.
    assert any(
        phrase in sent_text
        for phrase in ("error", "Error", "failed", "Failed", "unavailable", "Unavailable")
    ), f"Diagnostic message must mention the failure; got: {sent_text!r}"


@pytest.mark.asyncio
async def test_card_message_has_three_inline_buttons(monkeypatch):
    """Per-card buttons use the expected callback name convention.

    The legacy ``pulse_(up|down|save)_<id>`` callbacks were retired in favour
    of ``paper:feedback_pos:<id>:pulse_thumbs`` / ``paper:feedback_neg:...`` /
    ``paper:save:<id>``.
    """
    captured_keyboards: list = []

    def fake_markup(rows):
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

    with patch("telegram_bot.owner.list_user_pairings", AsyncMock(return_value=_DEFAULT_PAIRING)):
        await research_pulse.run_research_pulse(
            http_client,
            db_pool,
            bot,
            make_bot_config(BotConfig, jarvis_api_key=SecretStr("secret")),
        )

    assert len(captured_keyboards) == 2
    for rows in captured_keyboards:
        buttons = [b for row in rows for b in row]
        assert len(buttons) == 3
        callback_prefixes = sorted(b["callback_data"].rsplit(":", 1)[0] for b in buttons)
        # paper:feedback_neg:40:pulse_thumbs → paper:feedback_neg:40
        # paper:feedback_pos:40:pulse_thumbs → paper:feedback_pos:40
        # paper:save:40 → paper:save (no trailing source)
        # rsplit on ":" gives the prefix without the trailing source for feedback,
        # and the prefix without the id for save. Just assert all three are present.
        joined = " ".join(b["callback_data"] for b in buttons)
        assert "paper:feedback_pos:" in joined
        assert "paper:feedback_neg:" in joined
        assert "paper:save:" in joined
        # silence unused
        del callback_prefixes

    # First card's paper_id is 40 — every button should encode it.
    first_card_buttons = [b for row in captured_keyboards[0] for b in row]
    for button in first_card_buttons:
        assert ":40" in button["callback_data"]


@pytest.mark.asyncio
async def test_deck_is_capped_to_top_n():
    """A 20-card deck should only send up to PULSE_TELEGRAM_TOP_N cards."""
    bot = AsyncMock()
    http_client = AsyncMock(spec=httpx.AsyncClient)
    http_client.get.return_value = _ok_response(_make_deck(20))
    db_pool = AsyncMock()

    with patch("telegram_bot.owner.list_user_pairings", AsyncMock(return_value=_DEFAULT_PAIRING)):
        await research_pulse.run_research_pulse(
            http_client,
            db_pool,
            bot,
            make_bot_config(BotConfig, jarvis_api_key=SecretStr("secret")),
        )

    # PULSE_TELEGRAM_TOP_N is 5 (brevity). With optional header, up to 6 sends.
    assert bot.send_message.await_count <= research_pulse.PULSE_TELEGRAM_TOP_N + 1
    assert bot.send_message.await_count >= research_pulse.PULSE_TELEGRAM_TOP_N
