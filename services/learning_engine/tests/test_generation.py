"""Tests for H7 audit fix: user_id threading through generate_cards_core.

Covers:
  - generate_cards_core passes user_id to every insert_card call
  - _card_generate_batch_job scopes paper-pool query to user_library when user_id is set
  - _card_generate_batch_job uses the unscoped query when user_id is None
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jarvis_common.jobs import JobContext
from learning_engine import _state as le_state
from learning_engine.routers import generation


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


def _make_card_row(id=1, deck_id=1, paper_id=1, user_id=None):
    """Return a fake row compatible with row_to_card_response."""
    return FakeRecord(
        id=id,
        deck_id=deck_id,
        paper_id=paper_id,
        card_type="concept",
        front="What is X?",
        back="X is Y.",
        evidence={"quote": "X is Y", "page_number": 1},
        fsrs_state={},
        due_at=_now(),
        created_at=_now(),
        updated_at=_now(),
        user_id=user_id,
    )


def _make_ctx(job_id="test-job-h7"):
    ctx = MagicMock(spec=JobContext)
    ctx.job_id = job_id
    ctx.update_progress = AsyncMock()
    ctx.is_cancelled = AsyncMock(return_value=False)
    return ctx


# ---------------------------------------------------------------------------
# test_generate_cards_writes_user_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_cards_writes_user_id():
    """generate_cards_core passes user_id=42 to every insert_card call."""
    pool, conn = _make_pool_and_conn()
    http_client = AsyncMock()

    fsrs_manager = MagicMock()
    fsrs_manager.create_new_card.return_value = ({"state": "new"}, _now())

    card_generator = AsyncMock()
    card_generator.generate_cards.return_value = {
        "cards": [
            {
                "card_type": "concept",
                "front": "What is X?",
                "back": "X is Y.",
                "evidence": {"quote": "X is Y", "page_number": 1},
            },
            {
                "card_type": "cloze",
                "front": "_____ is Y.",
                "back": "X",
                "evidence": {"quote": "X is Y", "page_number": 1},
            },
        ],
        "confidence": "HIGH",
    }

    conn.fetchval.return_value = 1  # deck exists
    conn.fetchrow.return_value = FakeRecord(
        id=101, title="Paper 101", authors=["Ada"], abstract="A paper"
    )
    conn.fetch.return_value = [FakeRecord(id=1, content="chunk", page_number=1)]

    mock_insert = AsyncMock(
        side_effect=[
            _make_card_row(id=1, user_id=42),
            _make_card_row(id=2, user_id=42),
        ]
    )

    with (
        patch.object(generation, "get_smart_model", MagicMock(return_value="smart")),
        patch.object(generation, "insert_card", mock_insert),
        patch.object(le_state.svc, "openai_client", MagicMock()),
    ):
        result = await generation.generate_cards_core(
            pool=pool,
            http_client=http_client,
            paper_id=101,
            deck_id=1,
            max_cards=5,
            fsrs_manager=fsrs_manager,
            card_generator=card_generator,
            user_id=42,
        )

    assert result["cards_created"] == 2

    # Every insert_card call must have been given user_id=42
    assert mock_insert.await_count == 2
    for c in mock_insert.await_args_list:
        assert c.kwargs.get("user_id") == 42, f"insert_card called without user_id=42: {c}"


# ---------------------------------------------------------------------------
# test_batch_paper_pool_scopes_to_user_library
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_paper_pool_scopes_to_user_library():
    """_card_generate_batch_job uses user_library EXISTS clause when user_id is set."""
    pool, conn = _make_pool_and_conn()
    http_client = AsyncMock()
    ctx = _make_ctx()

    # Pool query returns 2 papers (simulating only user A's library papers)
    conn.fetch.return_value = [FakeRecord(id=10), FakeRecord(id=20)]

    mock_core = AsyncMock(return_value={"cards_created": 3, "cards": [], "confidence": "HIGH"})

    with patch.object(generation, "generate_cards_core", mock_core):
        result = await generation._card_generate_batch_job(
            pool=pool,
            http_client=http_client,
            payload={"deck_id": 5, "max_per_paper": 5, "user_id": 7},
            ctx=ctx,
        )

    assert result["papers_processed"] == 2
    assert result["cards_created"] == 6
    assert result["errors"] == []

    # The SQL passed to conn.fetch must include the user_library EXISTS clause
    fetch_call_sql: str = conn.fetch.await_args_list[0].args[0]
    assert "user_library" in fetch_call_sql, (
        "Expected user_library EXISTS clause in batch query when user_id is set"
    )
    assert "IS NOT DISTINCT FROM" in fetch_call_sql

    # generate_cards_core must have been called with user_id=7 for each paper
    assert mock_core.await_count == 2
    for c in mock_core.await_args_list:
        assert c.kwargs.get("user_id") == 7, f"generate_cards_core called without user_id=7: {c}"


# ---------------------------------------------------------------------------
# test_batch_paper_pool_no_user_id_uses_unscoped_query
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_paper_pool_no_user_id_uses_unscoped_query():
    """_card_generate_batch_job uses the unscoped query when user_id is None."""
    pool, conn = _make_pool_and_conn()
    http_client = AsyncMock()
    ctx = _make_ctx()

    conn.fetch.return_value = [FakeRecord(id=99)]

    mock_core = AsyncMock(return_value={"cards_created": 1, "cards": [], "confidence": "LOW"})

    with patch.object(generation, "generate_cards_core", mock_core):
        result = await generation._card_generate_batch_job(
            pool=pool,
            http_client=http_client,
            payload={"deck_id": 3, "max_per_paper": 5},  # no user_id key
            ctx=ctx,
        )

    assert result["papers_processed"] == 1

    fetch_call_sql: str = conn.fetch.await_args_list[0].args[0]
    assert "user_library" not in fetch_call_sql, (
        "user_library clause must NOT appear in unscoped query (user_id=None)"
    )

    # generate_cards_core called with user_id=None
    assert mock_core.await_count == 1
    assert mock_core.await_args_list[0].kwargs.get("user_id") is None
