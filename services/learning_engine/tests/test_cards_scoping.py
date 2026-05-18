"""RB-4: create_card IDOR fix — deck ownership enforcement.

Verifies that POST /api/cards rejects requests where the caller does NOT own
the target deck_id, returning 404 without inserting any card.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from learning_engine.models import CardCreate, CardType
from learning_engine.routers import cards

from tests.conftest import FakeRecord

# ---------------------------------------------------------------------------
# Helpers (mirrors test_cards_router.py pattern)
# ---------------------------------------------------------------------------


def _now():
    return datetime.now(UTC)


def _make_pool_conn(*, fetchval_return=None):
    """Build a mock asyncpg pool; conn.fetchval returns *fetchval_return*."""
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=fetchval_return)

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)

    pool = MagicMock()
    pool.acquire.return_value = ctx
    return pool, conn


def _make_card_row(id=1, deck_id=1, paper_id=None):
    return FakeRecord(
        id=id,
        deck_id=deck_id,
        paper_id=paper_id,
        card_type="concept",
        front="Q?",
        back="A.",
        evidence={},
        fsrs_state={},
        due_at=_now(),
        created_at=_now(),
        updated_at=_now(),
    )


# ---------------------------------------------------------------------------
# RB-4: deck ownership enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_card_rejects_other_users_deck() -> None:
    """User B POSTing with user A's deck_id must receive 404; insert_card must NOT be called.

    The deck ownership SELECT returns None (no row matching caller's user_id),
    which must raise HTTPException(404) before insert_card is ever attempted.
    """
    # fetchval returns None — deck exists but belongs to a different user.
    pool, conn = _make_pool_conn(fetchval_return=None)

    fsrs_manager = MagicMock()
    fsrs_manager.create_new_card.return_value = ({"state": "new"}, _now())

    with (
        patch.object(cards, "assert_paper_ownership", AsyncMock(return_value=None)),
        patch.object(cards, "insert_card", AsyncMock(return_value=_make_card_row())) as mock_insert,
    ):
        with pytest.raises(HTTPException) as exc_info:
            await cards.create_card.__wrapped__(
                SimpleNamespace(state=SimpleNamespace(user_id=2)),
                body=CardCreate(
                    deck_id=7,
                    paper_id=None,
                    card_type=CardType.CONCEPT,
                    front="Q?",
                    back="A.",
                ),
                db_pool=pool,
                fsrs_manager=fsrs_manager,
                user_id=2,
            )

    assert exc_info.value.status_code == 404
    assert "Deck not found" in exc_info.value.detail
    # The insert must never be called — no data written into another user's deck.
    mock_insert.assert_not_called()
    # The ownership SELECT must have been called with the correct arguments.
    conn.fetchval.assert_awaited_once_with(
        "SELECT id FROM decks WHERE id = $1 AND user_id = $2",
        7,
        2,
    )


@pytest.mark.asyncio
async def test_create_card_passes_for_own_deck() -> None:
    """User B POSTing with their own deck_id succeeds (fetchval returns deck id)."""
    pool, conn = _make_pool_conn(fetchval_return=5)  # owns deck 5

    fsrs_manager = MagicMock()
    fsrs_manager.create_new_card.return_value = ({"state": "new"}, _now())

    with (
        patch.object(cards, "assert_paper_ownership", AsyncMock(return_value=None)),
        patch.object(
            cards, "insert_card", AsyncMock(return_value=_make_card_row(deck_id=5))
        ) as mock_insert,
    ):
        response = await cards.create_card.__wrapped__(
            SimpleNamespace(state=SimpleNamespace(user_id=2)),
            body=CardCreate(
                deck_id=5,
                paper_id=None,
                card_type=CardType.CONCEPT,
                front="Q?",
                back="A.",
            ),
            db_pool=pool,
            fsrs_manager=fsrs_manager,
            user_id=2,
        )

    # insert_card was called — card was created.
    mock_insert.assert_awaited_once()
    assert response.deck_id == 5
