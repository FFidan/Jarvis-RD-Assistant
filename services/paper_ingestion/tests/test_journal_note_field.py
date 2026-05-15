"""Regression: the new optional ``note`` field on JournalPrompts (UI_v3 EOD).

Spec §3.10/§4.3 adds one optional free-note escape hatch to the EOD shutdown
ritual. It is an additive JSONB key — NO migration. These tests assert the new
field round-trips through the existing GET + POST-upsert journal route and that
omitting it is still valid (existing callers unaffected).
"""

from __future__ import annotations

from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from paper_ingestion.models.journal import JournalEntryCreate, JournalPrompts
from paper_ingestion.routers import my_day


def _make_pool_and_conn():
    conn = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx
    return pool, conn


def test_journal_prompts_note_optional_and_defaults_none():
    p = JournalPrompts()
    assert p.note is None
    # Existing callers that never set note are unaffected.
    assert JournalPrompts(first_move="x").note is None


def test_journal_prompts_note_excluded_when_none():
    """upsert uses model_dump(exclude_none=True); empty note must not be stored."""
    dumped = JournalPrompts(worked="shipped threads").model_dump(exclude_none=True)
    assert "note" not in dumped
    assert dumped == {"worked": "shipped threads"}


def test_journal_prompts_note_included_when_set():
    dumped = JournalPrompts(note="also fixed CI flake").model_dump(exclude_none=True)
    assert dumped == {"note": "also fixed CI flake"}


@pytest.mark.asyncio
async def test_upsert_journal_round_trips_note():
    pool, conn = _make_pool_and_conn()
    today = date.today()
    now = datetime.now()
    conn.fetchrow.return_value = {
        "id": 1,
        "date": today,
        "prompts": {"worked": "shipped", "note": "anything else here"},
        "created_at": now,
        "updated_at": now,
    }
    body = JournalEntryCreate(
        date=today,
        prompts=JournalPrompts(worked="shipped", note="anything else here"),
    )
    with patch(
        "paper_ingestion.routers.my_day.current_user_id_strict",
        new_callable=AsyncMock,
        return_value=None,
    ):
        result = await my_day.upsert_journal_entry.__wrapped__(MagicMock(), body=body, db_pool=pool)
    assert result.prompts.note == "anything else here"
    assert result.prompts.worked == "shipped"
    # The bound JSONB dict includes the note key (exclude_none kept it).
    _sql, *bound = conn.fetchrow.await_args.args
    assert bound[2] == {"worked": "shipped", "note": "anything else here"}
