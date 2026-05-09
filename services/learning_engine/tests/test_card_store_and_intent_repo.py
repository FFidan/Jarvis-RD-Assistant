"""Direct tests for card persistence and Today's Intent repository helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest


class _Acquire:
    """Async context manager returning a fake DB connection."""

    def __init__(self, conn: AsyncMock) -> None:
        self.conn = conn

    async def __aenter__(self) -> AsyncMock:
        return self.conn

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


def _pool_with_conn(conn: AsyncMock) -> MagicMock:
    pool = MagicMock()
    pool.acquire.return_value = _Acquire(conn)
    return pool


@pytest.mark.asyncio
async def test_insert_card_persists_all_card_fields() -> None:
    """insert_card should pass the complete card payload into one INSERT."""
    from learning_engine.card_store import insert_card

    conn = AsyncMock()
    row = {"id": 9}
    conn.fetchrow.return_value = row
    due_at = datetime(2026, 5, 9, 12, 0, tzinfo=UTC)

    result = await insert_card(
        conn,
        deck_id=1,
        paper_id=2,
        card_type="basic",
        front="Q",
        back="A",
        evidence={"quote": "evidence"},
        fsrs_state={"stability": 1.0},
        due_at=due_at,
    )

    assert result is row
    args = conn.fetchrow.await_args.args
    assert "INSERT INTO cards" in args[0]
    assert args[1:] == (
        1,
        2,
        "basic",
        "Q",
        "A",
        {"quote": "evidence"},
        {"stability": 1.0},
        due_at,
    )


@pytest.mark.asyncio
async def test_get_today_returns_empty_intent_when_no_row_exists() -> None:
    """Missing daily-intent rows should return the public empty shape."""
    from learning_engine.repos.intent_repo import get_today

    conn = AsyncMock()
    conn.fetchrow.return_value = None

    assert await get_today(_pool_with_conn(conn), user_id=7) == {
        "intent": None,
        "updated_at": None,
    }


@pytest.mark.asyncio
async def test_get_today_serializes_existing_row_timestamp() -> None:
    """Existing daily-intent rows should expose ISO timestamps."""
    from learning_engine.repos.intent_repo import get_today

    updated_at = datetime(2026, 5, 9, 9, 30, tzinfo=UTC)
    conn = AsyncMock()
    conn.fetchrow.return_value = {"intent_text": "focus", "updated_at": updated_at}

    assert await get_today(_pool_with_conn(conn), user_id=None) == {
        "intent": "focus",
        "updated_at": updated_at.isoformat(),
    }


@pytest.mark.asyncio
async def test_upsert_today_returns_upserted_intent() -> None:
    """Upserts should return the row emitted by the RETURNING clause."""
    from learning_engine.repos.intent_repo import upsert_today

    updated_at = datetime(2026, 5, 9, 10, 0, tzinfo=UTC)
    conn = AsyncMock()
    conn.fetchrow.return_value = {"intent_text": "ship tests", "updated_at": updated_at}

    result = await upsert_today(_pool_with_conn(conn), user_id=3, intent="ship tests")

    assert result == {"intent": "ship tests", "updated_at": updated_at.isoformat()}
    assert conn.fetchrow.await_args.args[1:] == (3, "ship tests")


@pytest.mark.asyncio
async def test_delete_today_executes_user_scoped_delete() -> None:
    """Deletes should scope by the supplied user id and current date."""
    from learning_engine.repos.intent_repo import delete_today

    conn = AsyncMock()

    await delete_today(_pool_with_conn(conn), user_id=11)

    args = conn.execute.await_args.args
    assert "DELETE FROM daily_intent" in args[0]
    assert args[1] == 11
