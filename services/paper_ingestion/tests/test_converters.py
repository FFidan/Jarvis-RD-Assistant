"""Tests for row_to_feed_paper and batch_hybrid_results_to_paper_responses converters.

Ensures that state and state_before_trash are forwarded from the SQL row
rather than silently falling back to Pydantic defaults, and that the batch
hybrid helper issues exactly one query, preserves RRF order, and uses the
deleted-paper fallback for ids not returned by the DB.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from paper_ingestion.converters import batch_hybrid_results_to_paper_responses, row_to_feed_paper
from tests._embedder_fakes import _dict_to_record

# ---------------------------------------------------------------------------
# Minimal row builder
# ---------------------------------------------------------------------------

_NOW = datetime.now(UTC)

_BASE_ROW: dict = {
    "id": 1,
    "external_id": "ext-001",
    "source_type": "arxiv",
    "title": "Test Paper",
    "authors": ["Alice", "Bob"],
    "abstract": "An abstract.",
    "published_date": _NOW.date(),
    "url": "https://example.com/paper",
    "pdf_url": None,
    "pdf_local_path": None,
    "pdf_downloaded": False,
    "citation_count": 0,
    "metadata": {},
    "created_at": _NOW,
}


def _row(**overrides) -> dict:
    """Return a copy of _BASE_ROW with any overrides applied."""
    return {**_BASE_ROW, **overrides}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_row_to_feed_paper_emits_state_when_present():
    """row with state='reading' → FeedPaper.state == 'reading'."""
    row = _row(state="reading")
    result = row_to_feed_paper(row)  # type: ignore[arg-type]
    assert result.state == "reading"


def test_row_to_feed_paper_emits_state_before_trash():
    """row with state='trash', state_before_trash='reading' → fields preserved."""
    row = _row(state="trash", state_before_trash="reading")
    result = row_to_feed_paper(row)  # type: ignore[arg-type]
    assert result.state == "trash"
    assert result.state_before_trash == "reading"


def test_row_to_feed_paper_defaults_when_state_keys_missing():
    """Row missing both state keys (legacy papers-only fetch) → inbox defaults."""
    row = _row()  # no state / state_before_trash keys
    result = row_to_feed_paper(row)  # type: ignore[arg-type]
    assert result.state == "inbox"
    assert result.state_before_trash is None


def test_row_to_feed_paper_discovery_origin_pulse():
    """Row with discovery_origin='pulse' → FeedPaper.discovery_origin == 'pulse'."""
    row = _row(discovery_origin="pulse")
    result = row_to_feed_paper(row)  # type: ignore[arg-type]
    assert result.discovery_origin == "pulse"


def test_row_to_feed_paper_discovery_origin_defaults_when_absent():
    """Row without discovery_origin key → FeedPaper.discovery_origin == 'user_initiated'."""
    row = _row()  # no discovery_origin key
    result = row_to_feed_paper(row)  # type: ignore[arg-type]
    assert result.discovery_origin == "user_initiated"


def test_row_to_feed_paper_recent_feedback_present():
    """Row with recent_feedback_signal/source/created_at → RecentFeedback populated."""
    row = _row(
        recent_feedback_signal="positive",
        recent_feedback_source="feed_thumbs",
        recent_feedback_created_at=_NOW,
    )
    result = row_to_feed_paper(row)  # type: ignore[arg-type]
    assert result.recent_feedback is not None
    assert result.recent_feedback.signal == "positive"
    assert result.recent_feedback.source == "feed_thumbs"
    assert result.recent_feedback.created_at == _NOW


def test_row_to_feed_paper_recent_feedback_absent():
    """Row without recent_feedback_* keys → FeedPaper.recent_feedback is None."""
    row = _row()  # no recent_feedback_* keys
    result = row_to_feed_paper(row)  # type: ignore[arg-type]
    assert result.recent_feedback is None


# ---------------------------------------------------------------------------
# batch_hybrid_results_to_paper_responses tests
# ---------------------------------------------------------------------------

_FULL_PAPER_ROW_DEFAULTS: dict = {
    "external_id": "ext-batch",
    "source_type": "arxiv",
    "authors": ["A"],
    "abstract": "abs",
    "published_date": None,
    "url": "https://example.com",
    "pdf_url": None,
    "pdf_local_path": None,
    "pdf_downloaded": False,
    "citation_count": 0,
    "priority_score": None,
    "metadata": {},
    "discovered_at": None,
    "created_at": _NOW,
}


def _paper_record(paper_id: int, title: str = "T") -> MagicMock:
    """Return a _dict_to_record-style fake asyncpg Record for the papers table."""
    d = {"id": paper_id, "title": title, **_FULL_PAPER_ROW_DEFAULTS}
    return _dict_to_record(d)


def _make_batch_pool(db_records: list[MagicMock]) -> tuple[AsyncMock, AsyncMock]:
    """Return (pool, conn) where conn.fetch returns db_records."""
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=db_records)
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = AsyncMock()
    pool.acquire = MagicMock(return_value=ctx)
    return pool, conn


@pytest.mark.asyncio
async def test_batch_fetch_called_exactly_once():
    """conn.fetch must be called exactly once with all ids (not per-row)."""
    hybrid_results = [{"id": 10}, {"id": 20}, {"id": 30}]
    records = [_paper_record(10, "P10"), _paper_record(20, "P20"), _paper_record(30, "P30")]
    pool, conn = _make_batch_pool(records)

    await batch_hybrid_results_to_paper_responses(hybrid_results, pool)

    conn.fetch.assert_awaited_once()
    _sql, fetched_ids = conn.fetch.await_args.args
    assert set(fetched_ids) == {10, 20, 30}


@pytest.mark.asyncio
async def test_batch_preserves_rrf_order():
    """Returned PaperResponse list must match the input/RRF order, not DB row order."""
    # RRF order: 30 → 10 → 20; DB returns in a different order: 10, 20, 30
    hybrid_results = [{"id": 30}, {"id": 10}, {"id": 20}]
    records = [_paper_record(10, "P10"), _paper_record(20, "P20"), _paper_record(30, "P30")]
    pool, _ = _make_batch_pool(records)

    responses = await batch_hybrid_results_to_paper_responses(hybrid_results, pool)

    assert [r.id for r in responses] == [30, 10, 20]


@pytest.mark.asyncio
async def test_batch_deleted_paper_fallback():
    """An id missing from the DB result uses the deleted-paper fallback shape."""
    hybrid_results = [
        {"id": 1, "title": "Alive", "authors": ["A"], "url": "https://a.com"},
        {
            "id": 999,
            "title": "Gone",
            "authors": ["B"],
            "url": "https://gone.com",
            "abstract": "deleted abstract",
            "published_date": None,
        },
    ]
    # DB only returns the paper with id=1; id=999 has been deleted
    records = [_paper_record(1, "Alive")]
    pool, _ = _make_batch_pool(records)

    responses = await batch_hybrid_results_to_paper_responses(hybrid_results, pool)

    assert len(responses) == 2
    assert responses[0].id == 1
    assert responses[0].title == "Alive"

    fallback = responses[1]
    assert fallback.id == 999
    assert fallback.title == "Gone"
    assert fallback.url == "https://gone.com"
    assert fallback.external_id == ""
    assert fallback.created_at is not None
