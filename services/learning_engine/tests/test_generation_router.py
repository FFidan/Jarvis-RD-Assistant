"""Direct tests for the card generation router."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "libs" / "jarvis_common"))

from app.models import BatchGenerateRequest, GenerateCardsRequest  # noqa: E402
from app.routers import generation  # noqa: E402
from jarvis_common.db_helpers import get_smart_model  # noqa: E402


class FakeRecord(dict):
    """Dict-like asyncpg.Record substitute."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def get(self, key, default=None):
        return super().get(key, default)


def _now():
    return datetime.now(UTC)


def _make_pool_and_conn():
    """Create a mock pool whose acquire() returns an async context manager."""
    conn = AsyncMock()

    txn_cm = MagicMock()
    txn_cm.__aenter__ = AsyncMock(return_value=txn_cm)
    txn_cm.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn_cm)

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)

    pool = MagicMock()
    pool.acquire.return_value = ctx
    return pool, conn


def _make_card_row(id=1, deck_id=1, paper_id=1):
    """Return a fake row compatible with row_to_card_response."""
    return FakeRecord(
        id=id,
        deck_id=deck_id,
        paper_id=paper_id,
        card_type="concept",
        front="What changed?",
        back="The method improved retrieval.",
        evidence={"quote": "Improved retrieval", "page_number": 2},
        fsrs_state={},
        due_at=_now(),
        created_at=_now(),
        updated_at=_now(),
    )


def test_get_smart_model_returns_alias():
    """get_smart_model always returns the 'smart' LiteLLM alias."""
    assert get_smart_model() == "smart"


def test_get_smart_model_no_conn_param():
    """get_smart_model requires no arguments (conn was removed)."""
    assert get_smart_model() == "smart"


@pytest.mark.asyncio
async def test_generate_cards_success_creates_cards_and_uses_validated_model():
    """generate_cards inserts verified cards and returns the response model."""
    pool, conn = _make_pool_and_conn()
    fsrs_manager = MagicMock()
    fsrs_manager.create_new_card.return_value = ({"state": "new"}, _now())
    card_generator = AsyncMock()
    card_generator.generate_cards.return_value = {
        "cards": [
            {
                "card_type": "concept",
                "front": "What changed?",
                "back": "The method improved retrieval.",
                "evidence": {"quote": "Improved retrieval", "page_number": 2},
            }
        ],
        "confidence": "HIGH",
    }

    conn.fetchval.return_value = 1  # deck exists check
    conn.fetchrow.return_value = FakeRecord(
        id=101,
        title="Paper 101",
        authors=["Ada"],
        abstract="A paper",
    )
    conn.fetch.return_value = [FakeRecord(id=1, content="chunk", page_number=2)]

    with (
        patch.object(generation, "get_smart_model", MagicMock(return_value="resolved-model")) as mock_get_model,
        patch.object(generation, "_insert_card", AsyncMock(return_value=_make_card_row(id=501, paper_id=101))),
    ):
        response = await generation.generate_cards.__wrapped__(
            MagicMock(),
            body=GenerateCardsRequest(paper_id=101, deck_id=1),
            db_pool=pool,
            fsrs_manager=fsrs_manager,
            card_generator=card_generator,
        )

    assert response.cards_created == 1
    assert response.confidence == "HIGH"
    assert response.cards[0].paper_id == 101
    mock_get_model.assert_called_once()
    card_generator.generate_cards.assert_awaited_once()
    assert card_generator.generate_cards.await_args.kwargs["model"] == "resolved-model"


@pytest.mark.asyncio
async def test_generate_cards_deck_not_found():
    """generate_cards returns 404 before touching the generator when the deck is missing."""
    pool, conn = _make_pool_and_conn()

    conn.fetchval.return_value = None

    with pytest.raises(HTTPException, match="Deck not found") as exc_info:
        await generation.generate_cards.__wrapped__(
            MagicMock(),
            body=GenerateCardsRequest(paper_id=101, deck_id=999),
            db_pool=pool,
            fsrs_manager=MagicMock(),
            card_generator=AsyncMock(),
        )

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_generate_cards_maps_generator_failure_to_502():
    """generate_cards hides internal generator failures behind a stable 502."""
    pool, conn = _make_pool_and_conn()
    fsrs_manager = MagicMock()
    card_generator = AsyncMock()
    card_generator.generate_cards.side_effect = RuntimeError("boom")

    conn.fetchval.return_value = 1  # deck exists check
    conn.fetchrow.return_value = FakeRecord(
        id=101,
        title="Paper 101",
        authors=["Ada"],
        abstract="A paper",
    )
    conn.fetch.return_value = [FakeRecord(id=1, content="chunk", page_number=2)]

    with (
        patch.object(generation, "get_smart_model", AsyncMock(return_value="smart")),
        pytest.raises(HTTPException, match="Card generation failed") as exc_info,
    ):
        await generation.generate_cards.__wrapped__(
            MagicMock(),
            body=GenerateCardsRequest(paper_id=101, deck_id=1),
            db_pool=pool,
            fsrs_manager=fsrs_manager,
            card_generator=card_generator,
        )

    assert exc_info.value.status_code == 502


@pytest.mark.asyncio
async def test_batch_generate_records_missing_metadata_errors():
    """batch_generate_cards leaves a readable error when a paper row vanishes mid-loop."""
    pool, conn = _make_pool_and_conn()
    fsrs_manager = MagicMock()
    card_generator = AsyncMock()

    conn.fetchval.side_effect = [1, None]
    conn.fetch.side_effect = [[FakeRecord(id=101)], [FakeRecord(id=1, content="chunk", page_number=2)]]
    conn.fetchrow.return_value = None

    response = await generation.batch_generate_cards.__wrapped__(
        MagicMock(),
        body=BatchGenerateRequest(deck_id=1),
        db_pool=pool,
        fsrs_manager=fsrs_manager,
        card_generator=card_generator,
    )

    assert response.papers_processed == 0
    assert response.cards_created == 0
    assert response.errors == ["Paper 101: missing metadata or chunks"]
    card_generator.generate_cards.assert_not_called()
