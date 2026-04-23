"""Direct tests for the card CRUD router."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from learning_engine.models import CardCreate, CardType, CardUpdate, Evidence  # noqa: E402
from learning_engine.routers import cards  # noqa: E402


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


@pytest.mark.asyncio
async def test_create_card_success_uses_evidence_payload():
    """create_card serializes nested evidence before inserting the card."""
    pool, conn = _make_pool_and_conn()
    fsrs_manager = MagicMock()
    fsrs_manager.create_new_card.return_value = ({"state": "new"}, _now())

    with patch.object(
        cards, "insert_card", AsyncMock(return_value=_make_card_row(id=5, paper_id=7))
    ) as mock_insert:
        response = await cards.create_card.__wrapped__(
            MagicMock(),
            body=CardCreate(
                deck_id=1,
                paper_id=7,
                card_type=CardType.CONCEPT,
                front="Q?",
                back="A.",
                evidence=Evidence(quote="A", page_number=1),
            ),
            db_pool=pool,
            fsrs_manager=fsrs_manager,
        )

    assert response.id == 5
    assert response.paper_id == 7
    assert mock_insert.await_args is not None
    assert mock_insert.await_args.args[6] == {
        "quote": "A",
        "page_number": 1,
        "chunk_id": None,
        "snapshot_path": None,
        "verified": True,
    }


@pytest.mark.asyncio
async def test_list_cards_builds_query_with_filters():
    """list_cards includes deck and due filters in the generated SQL."""
    pool, conn = _make_pool_and_conn()
    due_before = _now()
    conn.fetch.return_value = [_make_card_row()]

    rows = await cards.list_cards.__wrapped__(
        MagicMock(),
        deck_id=3,
        due_before=due_before,
        limit=10,
        offset=5,
        db_pool=pool,
    )

    assert len(rows) == 1
    sql = conn.fetch.await_args.args[0]
    params = conn.fetch.await_args.args[1:]
    assert "deck_id = $1" in sql
    assert "due_at <= $2" in sql
    assert params == (3, due_before, 10, 5)


@pytest.mark.asyncio
async def test_update_card_returns_existing_row_when_body_is_empty():
    """update_card is a no-op when no fields change: first fetchrow checks existence,
    second fetchrow returns the full row."""
    pool, conn = _make_pool_and_conn()
    card_row = _make_card_row(id=9)
    # First call: SELECT id (existence check), second call: SELECT * (no-op fetch)
    conn.fetchrow.side_effect = [card_row, card_row]

    response = await cards.update_card.__wrapped__(
        MagicMock(),
        card_id=9,
        body=CardUpdate(),
        db_pool=pool,
    )

    assert response.id == 9
    assert conn.fetchrow.await_count == 2


@pytest.mark.asyncio
async def test_update_card_raises_404_when_missing():
    """update_card returns 404 when the target card does not exist."""
    pool, conn = _make_pool_and_conn()
    # First fetchrow: existence check returns None → 404 raised before dynamic_update
    conn.fetchrow.return_value = None

    with pytest.raises(HTTPException, match="Card not found") as exc_info:
        await cards.update_card.__wrapped__(
            MagicMock(),
            card_id=999,
            body=CardUpdate(front="Updated"),
            db_pool=pool,
        )

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_update_card_uses_dynamic_update():
    """update_card delegates to dynamic_update and returns the updated row."""
    pool, conn = _make_pool_and_conn()
    existing = FakeRecord(id=7)
    updated_row = _make_card_row(id=7)
    # fetchrow calls: existence check, then dynamic_update internally calls fetchrow
    conn.fetchrow.side_effect = [existing, updated_row]

    with patch(
        "learning_engine.routers.cards.dynamic_update", AsyncMock(return_value=updated_row)
    ) as mock_du:
        response = await cards.update_card.__wrapped__(
            MagicMock(),
            card_id=7,
            body=CardUpdate(front="New front", back="New back"),
            db_pool=pool,
        )

    assert response.id == 7
    mock_du.assert_awaited_once()
    call_kwargs = mock_du.await_args.kwargs
    assert call_kwargs["table"] == "cards"
    assert call_kwargs["record_id"] == 7
    assert call_kwargs["updates"] == {"front": "New front", "back": "New back"}
    assert "evidence" in call_kwargs["jsonb_columns"]
    assert "updated_at = NOW()" in call_kwargs["extra_sets"]


@pytest.mark.asyncio
async def test_delete_card_raises_404_when_row_missing():
    """delete_card returns 404 when the DELETE statement affects no rows."""
    pool, conn = _make_pool_and_conn()
    conn.execute.return_value = "DELETE 0"

    with pytest.raises(HTTPException, match="Card not found") as exc_info:
        await cards.delete_card.__wrapped__(
            MagicMock(),
            card_id=999,
            db_pool=pool,
        )

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_create_card_raises_404_on_fk_violation_deck():
    """create_card maps ForeignKeyViolationError (deck constraint) to 404."""
    import asyncpg

    pool, conn = _make_pool_and_conn()
    fsrs_manager = MagicMock()
    fsrs_manager.create_new_card.return_value = ({"state": "new"}, _now())

    exc = asyncpg.ForeignKeyViolationError()
    setattr(exc, "constraint_name", "cards_deck_id_fkey")  # type: ignore[attr-defined]

    with patch.object(cards, "insert_card", AsyncMock(side_effect=exc)):
        with pytest.raises(HTTPException, match="Deck not found") as exc_info:
            await cards.create_card.__wrapped__(
                MagicMock(),
                body=CardCreate(
                    deck_id=99,
                    card_type=CardType.CONCEPT,
                    front="Q?",
                    back="A.",
                ),
                db_pool=pool,
                fsrs_manager=fsrs_manager,
            )

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_list_cards_no_filters_omits_where_clause():
    """list_cards without any filter issues a plain SELECT with no WHERE."""
    pool, conn = _make_pool_and_conn()
    conn.fetch.return_value = []

    result = await cards.list_cards.__wrapped__(
        MagicMock(),
        deck_id=None,
        due_before=None,
        limit=20,
        offset=0,
        db_pool=pool,
    )

    assert result == []
    sql = conn.fetch.await_args.args[0]
    assert "WHERE" not in sql
