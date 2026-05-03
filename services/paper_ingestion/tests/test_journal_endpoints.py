"""Unit tests for journal entry CRUD endpoints (Phase 2 My Day).

Uses the __wrapped__ pattern (same as test_star_zotero_push_trigger.py) to
call the endpoint functions directly, bypassing FastAPI routing and the
slowapi rate-limiter decorator.
"""

from __future__ import annotations

from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from paper_ingestion.routers import my_day

# ---------------------------------------------------------------------------
# Helpers — mirror test_star_zotero_push_trigger.py pattern
# ---------------------------------------------------------------------------


def _make_pool_and_conn():
    """Return a (pool, conn) pair backed by AsyncMock."""
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


def _mock_request():
    return MagicMock()


# ---------------------------------------------------------------------------
# GET /api/my-day/journal — entry found
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_journal_entry_found():
    """fetchrow returns a row → endpoint returns JournalEntryResponse."""
    pool, conn = _make_pool_and_conn()
    today = date.today()
    now = datetime.now()
    conn.fetchrow.return_value = {
        "id": 1,
        "date": today,
        "prompts": {"first_move": "Write tests"},
        "created_at": now,
        "updated_at": now,
    }
    with patch(
        "paper_ingestion.routers.my_day.current_user_id_or_none",
        new_callable=AsyncMock,
        return_value=None,
    ):
        result = await my_day.get_journal_entry.__wrapped__(
            _mock_request(), date=str(today), db_pool=pool
        )
    assert result.id == 1
    assert result.prompts.first_move == "Write tests"
    assert result.date == today


# ---------------------------------------------------------------------------
# GET /api/my-day/journal — entry not found (404)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_journal_entry_not_found():
    """fetchrow returns None → endpoint raises HTTPException 404."""
    from fastapi import HTTPException

    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = None
    with patch(
        "paper_ingestion.routers.my_day.current_user_id_or_none",
        new_callable=AsyncMock,
        return_value=None,
    ):
        with pytest.raises(HTTPException) as exc_info:
            await my_day.get_journal_entry.__wrapped__(
                _mock_request(), date="2026-05-01", db_pool=pool
            )
    assert exc_info.value.status_code == 404
    assert "not found" in exc_info.value.detail.lower()


# ---------------------------------------------------------------------------
# POST /api/my-day/journal — creates / updates entry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_journal_entry_creates():
    """fetchrow returns upserted row → endpoint returns JournalEntryResponse."""
    from paper_ingestion.models.journal import JournalEntryCreate, JournalPrompts

    pool, conn = _make_pool_and_conn()
    today = date.today()
    now = datetime.now()
    conn.fetchrow.return_value = {
        "id": 1,
        "date": today,
        "prompts": {"first_move": "Ship it"},
        "created_at": now,
        "updated_at": now,
    }
    body = JournalEntryCreate(date=today, prompts=JournalPrompts(first_move="Ship it"))
    with patch(
        "paper_ingestion.routers.my_day.current_user_id_or_none",
        new_callable=AsyncMock,
        return_value=None,
    ):
        result = await my_day.upsert_journal_entry.__wrapped__(
            _mock_request(), body=body, db_pool=pool
        )
    assert result.prompts.first_move == "Ship it"
    assert result.id == 1
    conn.fetchrow.assert_awaited_once()


# ---------------------------------------------------------------------------
# POST /api/my-day/journal — empty prompts round-trips correctly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_journal_entry_empty_prompts():
    """Empty JournalPrompts serialises to {} and returns without error."""
    from paper_ingestion.models.journal import JournalEntryCreate, JournalPrompts

    pool, conn = _make_pool_and_conn()
    today = date.today()
    now = datetime.now()
    conn.fetchrow.return_value = {
        "id": 2,
        "date": today,
        "prompts": {},
        "created_at": now,
        "updated_at": now,
    }
    body = JournalEntryCreate(date=today, prompts=JournalPrompts())
    with patch(
        "paper_ingestion.routers.my_day.current_user_id_or_none",
        new_callable=AsyncMock,
        return_value=None,
    ):
        result = await my_day.upsert_journal_entry.__wrapped__(
            _mock_request(), body=body, db_pool=pool
        )
    assert result.prompts.first_move is None
    assert result.prompts.worked is None
    assert result.prompts.blocked is None
