"""Tests for H6: Anki export ownership scoping.

Verifies that GET /api/export/anki/{deck_id} returns 404 for decks
owned by a different user and returns a valid .apkg for the owner.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from learning_engine.routers.export import export_anki

from tests.conftest import _make_pool_and_conn

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_request(user_id: int | None = None) -> SimpleNamespace:
    """Return a minimal request stand-in with request.state.user_id set."""
    return SimpleNamespace(state=SimpleNamespace(user_id=user_id))


# ---------------------------------------------------------------------------
# H6: ownership enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_export_anki_returns_404_for_other_users_deck() -> None:
    """A user requesting another user's deck must receive 404, not the apkg."""
    # Simulate DB returning no row because user_id does not match.
    pool, _conn = _make_pool_and_conn(fetchrow_return=None)
    fake_anki = MagicMock()

    with pytest.raises(HTTPException) as exc_info:
        await export_anki.__wrapped__(
            _fake_request(user_id=99),
            deck_id=1,
            db_pool=pool,
            anki_exporter=fake_anki,
        )

    assert exc_info.value.status_code == 404
    assert "Deck not found" in exc_info.value.detail
    # The anki exporter must never be called — no data leaked.
    fake_anki.export_deck.assert_not_called()


@pytest.mark.asyncio
async def test_export_anki_passes_user_id_to_deck_query() -> None:
    """The deck SELECT must bind the caller's user_id as the second parameter."""
    pool, conn = _make_pool_and_conn(fetchrow_return=None)
    fake_anki = MagicMock()

    with pytest.raises(HTTPException):
        await export_anki.__wrapped__(
            _fake_request(user_id=42),
            deck_id=7,
            db_pool=pool,
            anki_exporter=fake_anki,
            user_id=42,
        )

    # Verify the SQL call carried user_id=42 as the second bind arg.
    conn.fetchrow.assert_awaited_once()
    call_args = conn.fetchrow.call_args
    positional_args = call_args.args
    assert positional_args[1] == 7, "first bind arg should be deck_id"
    assert positional_args[2] == 42, "second bind arg should be user_id"
    assert "user_id = $2" in positional_args[0]


@pytest.mark.asyncio
async def test_export_anki_returns_apkg_for_owner() -> None:
    """Deck owner receives a StreamingResponse with the .apkg content."""
    fake_deck = {"id": 1, "name": "My Deck", "user_id": 5}
    fake_cards = [
        {
            "front": "Q1",
            "back": "A1",
            "evidence": {},
            "paper_title": "Paper One",
            "paper_authors": ["Smith, J."],
            "deck_id": 1,
            "created_at": "2026-01-01",
        }
    ]
    pool, _conn = _make_pool_and_conn(fetchrow_return=fake_deck, fetch_return=fake_cards)

    fake_anki = MagicMock()
    fake_anki.export_deck.return_value = b"APKG_BYTES"

    response = await export_anki.__wrapped__(
        _fake_request(user_id=5),
        deck_id=1,
        db_pool=pool,
        anki_exporter=fake_anki,
    )

    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    assert response.headers["content-disposition"].endswith('.apkg"')
    fake_anki.export_deck.assert_called_once()
    call_name, call_cards = fake_anki.export_deck.call_args.args
    assert call_name == "My Deck"
    assert call_cards[0]["front"] == "Q1"


@pytest.mark.asyncio
async def test_export_anki_returns_404_for_empty_deck_when_no_cards() -> None:
    """A deck with no cards returns 400 (existing contract), not 200."""
    fake_deck = {"id": 2, "name": "Empty Deck", "user_id": 5}
    pool, _conn = _make_pool_and_conn(fetchrow_return=fake_deck, fetch_return=[])

    fake_anki = MagicMock()

    with pytest.raises(HTTPException) as exc_info:
        await export_anki.__wrapped__(
            _fake_request(user_id=5),
            deck_id=2,
            db_pool=pool,
            anki_exporter=fake_anki,
        )

    assert exc_info.value.status_code == 400
    assert "no cards" in exc_info.value.detail
