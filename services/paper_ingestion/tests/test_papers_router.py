"""Direct tests for high-risk papers router branches."""

from __future__ import annotations

import sys
import types
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "libs" / "jarvis_common"))

if "fitz" not in sys.modules:
    sys.modules["fitz"] = MagicMock()
if "tiktoken" not in sys.modules:
    fake_tiktoken = types.ModuleType("tiktoken")
    fake_tiktoken.get_encoding = MagicMock(return_value=MagicMock())
    sys.modules["tiktoken"] = fake_tiktoken
if "qdrant_client" not in sys.modules:
    fake_qdrant = types.ModuleType("qdrant_client")
    fake_qdrant.AsyncQdrantClient = MagicMock()
    sys.modules["qdrant_client"] = fake_qdrant
if "qdrant_client.models" not in sys.modules:
    fake_qdrant_models = types.ModuleType("qdrant_client.models")
    fake_qdrant_models.Distance = MagicMock()
    fake_qdrant_models.PointIdsList = MagicMock()
    fake_qdrant_models.PointStruct = MagicMock()
    fake_qdrant_models.VectorParams = MagicMock()
    sys.modules["qdrant_client.models"] = fake_qdrant_models

from app.models import (  # noqa: E402
    Confidence,
    CrossReference,
    KeyFinding,
    PaperCreate,
    PaperResponse,
    PaperStatus,
    SummaryResponse,
)
from app.routers import papers  # noqa: E402


class FakeRecord(dict):
    """Dict-like asyncpg.Record substitute."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def get(self, key, default=None):
        return super().get(key, default)


def _make_pool_and_conn():
    """Create a mock pool whose acquire() returns an async context manager."""
    conn = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx
    return pool, conn


def _paper_row(id=1):
    """Return a minimal paper row for converter-backed responses."""
    return FakeRecord(
        id=id,
        external_id=f"paper-{id}",
        source_type="arxiv",
        title=f"Paper {id}",
        authors=["Ada"],
        abstract="Abstract",
        published_date=None,
        url=f"https://example.com/papers/{id}",
        pdf_url=None,
        citation_count=0,
        metadata={},
        pdf_local_path=None,
        pdf_downloaded=False,
        is_read=False,
        discovered_at=None,
        priority_score=None,
        created_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_list_papers_with_new_status_uses_left_join_filter():
    """The implicit NEW status should include papers without user_state rows."""
    pool, conn = _make_pool_and_conn()
    conn.fetch.return_value = [_paper_row()]

    rows = await papers.list_papers.__wrapped__(
        SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None))),
        status=PaperStatus.NEW,
        source_type=None,
        topic_id=None,
        q=None,
        limit=20,
        offset=0,
        db_pool=pool,
    )

    assert len(rows) == 1
    sql = conn.fetch.await_args.args[0]
    assert "LEFT JOIN paper_user_state pus" in sql
    assert "pus.paper_id IS NULL" in sql


@pytest.mark.asyncio
async def test_get_paper_detail_raises_404_when_missing():
    """get_paper_detail returns 404 when the paper row is absent."""
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = None

    with pytest.raises(HTTPException, match="Paper not found") as exc_info:
        await papers.get_paper_detail.__wrapped__(
            MagicMock(),
            paper_id=999,
            db_pool=pool,
        )

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_get_paper_detail_returns_summary_chunks_and_user_state():
    """get_paper_detail should compose the converted nested response payload."""
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.side_effect = [
        _paper_row(id=3),
        {"id": 10},
        {"status": "reading", "rating": 4, "user_notes": "Important", "flagged": False},
    ]
    conn.fetch.return_value = [FakeRecord(id=1)]

    paper_model = PaperResponse(
        id=3,
        external_id="paper-3",
        source_type="arxiv",
        title="Paper 3",
        authors=["Ada"],
        url="https://example.com/papers/3",
        created_at=datetime.now(UTC),
    )
    summary_model = SummaryResponse(
        id=10,
        paper_id=3,
        summary_brief="Brief",
        summary_detailed="Detailed",
        key_findings=[KeyFinding(finding="Claim", quote="Quote")],
        confidence=Confidence.HIGH,
        cross_references=[CrossReference(related_paper_id=4, relationship="extends", explanation="related")],
        created_at=datetime.now(UTC),
    )

    with (
        patch.object(papers, "row_to_paper_response", return_value=paper_model) as paper_conv,
        patch.object(papers, "row_to_summary_response", return_value=summary_model) as summary_conv,
        patch.object(papers, "row_to_chunk_response", return_value=FakeRecord(id=1, paper_id=3, chunk_index=0, content="chunk", created_at=datetime.now(UTC))) as chunk_conv,
    ):
        result = await papers.get_paper_detail.__wrapped__(
            MagicMock(),
            paper_id=3,
            db_pool=pool,
        )

    assert result.paper.id == 3
    assert result.summary.id == 10
    assert result.chunks[0].id == 1
    assert result.user_state.status == "reading"
    paper_conv.assert_called_once()
    summary_conv.assert_called_once()
    chunk_conv.assert_called_once()


@pytest.mark.asyncio
async def test_mark_paper_read_updates_both_legacy_and_user_state_tables():
    """mark_paper_read should update papers.is_read and upsert paper_user_state."""
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = {"id": 7}

    result = await papers.mark_paper_read.__wrapped__(
        MagicMock(),
        paper_id=7,
        db_pool=pool,
    )

    assert result == {"status": "ok", "paper_id": 7}
    assert conn.execute.await_count == 1
    sql = conn.execute.await_args.args[0]
    assert "INSERT INTO paper_user_state" in sql


@pytest.mark.asyncio
async def test_list_papers_search_query_uses_bm25_clause():
    """list_papers should add the search_vector clause on BM25 fallback queries."""
    pool, conn = _make_pool_and_conn()
    conn.fetch.return_value = [_paper_row()]

    rows = await papers.list_papers.__wrapped__(
        SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None))),
        status=None,
        source_type=None,
        topic_id=None,
        q="attention",
        limit=10,
        offset=0,
        db_pool=pool,
    )

    assert len(rows) == 1
    sql = conn.fetch.await_args.args[0]
    assert "search_vector @@ plainto_tsquery" in sql


@pytest.mark.asyncio
async def test_batch_save_rejects_oversized_requests():
    """batch_save_papers should reject requests over the documented batch limit."""
    pool, _ = _make_pool_and_conn()
    papers_payload = [
        PaperCreate(
            external_id=f"paper-{i}",
            source_type="arxiv",
            title=f"Paper {i}",
            authors=["Ada"],
            url=f"https://example.com/{i}",
        )
        for i in range(101)
    ]

    with pytest.raises(HTTPException, match="Batch size cannot exceed 100"):
        await papers.batch_save_papers.__wrapped__(
            MagicMock(),
            papers=papers_payload,
            db_pool=pool,
        )


@pytest.mark.asyncio
async def test_batch_save_returns_empty_list_for_empty_payload():
    """batch_save_papers should no-op on empty input."""
    pool, _ = _make_pool_and_conn()

    result = await papers.batch_save_papers.__wrapped__(
        MagicMock(),
        papers=[],
        db_pool=pool,
    )

    assert result == []


@pytest.mark.asyncio
async def test_submit_feedback_requires_rating_or_flagged():
    """submit_feedback should reject empty updates instead of writing blank state."""
    with pytest.raises(HTTPException, match="At least one of 'rating' or 'flagged' must be provided."):
        await papers.submit_feedback.__wrapped__(
            SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db_pool=MagicMock()))),
            paper_id=7,
            rating=None,
            flagged=None,
        )


@pytest.mark.asyncio
async def test_submit_feedback_maps_foreign_key_violation_to_404():
    """submit_feedback should convert FK errors into a stable 404."""
    conn = AsyncMock()
    conn.execute.side_effect = asyncpg.ForeignKeyViolationError("missing paper")
    pool, _ = _make_pool_and_conn()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db_pool=pool)))

    with pytest.raises(HTTPException, match="Paper 7 not found") as exc_info:
        await papers.submit_feedback.__wrapped__(
            request,
            paper_id=7,
            rating=5,
            flagged=None,
            db_pool=pool,
        )

    assert exc_info.value.status_code == 404
