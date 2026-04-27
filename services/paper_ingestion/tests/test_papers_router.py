"""Direct tests for high-risk papers router branches."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest
from fastapi import HTTPException

# conftest.py has already installed tiktoken / qdrant_client / qdrant_client.models stubs.
from paper_ingestion.models import (  # noqa: E402
    Confidence,
    CrossReference,
    KeyFinding,
    PaperCreate,
    PaperResponse,
    PaperStatus,
    SourceType,
    SummaryResponse,
)
from paper_ingestion.routers import papers  # noqa: E402


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
    conn.fetchval = AsyncMock(return_value=2)

    paper_model = PaperResponse(
        id=3,
        external_id="paper-3",
        source_type=SourceType.ARXIV,
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
        cross_references=[
            CrossReference(related_paper_id=4, relationship="extends", explanation="related")
        ],
        created_at=datetime.now(UTC),
    )

    with (
        patch.object(papers, "row_to_paper_response", return_value=paper_model) as paper_conv,
        patch.object(papers, "row_to_summary_response", return_value=summary_model) as summary_conv,
        patch.object(
            papers,
            "row_to_chunk_response",
            return_value=FakeRecord(
                id=1, paper_id=3, chunk_index=0, content="chunk", created_at=datetime.now(UTC)
            ),
        ) as chunk_conv,
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
    assert result.has_project_links is True
    paper_conv.assert_called_once()
    summary_conv.assert_called_once()
    chunk_conv.assert_called_once()
    conn.fetchval.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_paper_detail_sets_has_project_links_false_when_unlinked():
    """get_paper_detail should expose a false project-link flag when count is zero."""
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.side_effect = [
        _paper_row(id=4),
        None,
        None,
    ]
    conn.fetch.return_value = []
    conn.fetchval = AsyncMock(return_value=0)

    paper_model = PaperResponse(
        id=4,
        external_id="paper-4",
        source_type=SourceType.ARXIV,
        title="Paper 4",
        authors=["Ada"],
        url="https://example.com/papers/4",
        created_at=datetime.now(UTC),
    )

    with patch.object(papers, "row_to_paper_response", return_value=paper_model):
        result = await papers.get_paper_detail.__wrapped__(
            MagicMock(),
            paper_id=4,
            db_pool=pool,
        )

    assert result.has_project_links is False
    conn.fetchval.assert_awaited_once()


@pytest.mark.asyncio
async def test_mark_paper_read_updates_both_legacy_and_user_state_tables():
    """mark_paper_read should verify paper existence and upsert paper_user_state."""
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
        embedder=None,
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
            source_type=SourceType.ARXIV,
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
    from paper_ingestion.models import FeedbackRequest

    with pytest.raises(
        HTTPException, match="At least one of 'rating' or 'flagged' must be provided."
    ):
        await papers.submit_feedback.__wrapped__(
            SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db_pool=MagicMock()))),
            paper_id=7,
            feedback=FeedbackRequest(rating=None, flagged=None),
        )


@pytest.mark.asyncio
async def test_submit_feedback_maps_foreign_key_violation_to_404():
    """submit_feedback should convert FK errors into a stable 404."""
    from paper_ingestion.models import FeedbackRequest

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
            feedback=FeedbackRequest(rating=5, flagged=None),
            db_pool=pool,
        )

    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# PI-009: list_papers positional parameter correctness
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_paper_bookmark_creates_state_row():
    """bookmark_paper should verify paper existence and upsert paper_user_state with 'starred'."""
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = {"id": 5}
    conn.fetchval = AsyncMock(return_value=None)  # no prior state → will star

    result = await papers.bookmark_paper.__wrapped__(
        MagicMock(),
        paper_id=5,
        db_pool=pool,
    )

    assert result == {"status": "ok", "paper_id": 5}
    assert conn.execute.await_count == 1
    sql = conn.execute.await_args.args[0]
    assert "INSERT INTO paper_user_state" in sql
    # new_status='starred' is passed as $3 parameter, not hardcoded in SQL
    execute_args = conn.execute.await_args.args
    assert "starred" in execute_args  # third positional arg is the status


@pytest.mark.asyncio
async def test_put_paper_bookmark_idempotent_on_repeat():
    """bookmark_paper is idempotent — second call still returns ok and executes upsert."""
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = {"id": 5}
    conn.fetchval = AsyncMock(return_value=None)  # no prior state

    # Call twice — both should succeed
    result1 = await papers.bookmark_paper.__wrapped__(
        MagicMock(),
        paper_id=5,
        db_pool=pool,
    )
    result2 = await papers.bookmark_paper.__wrapped__(
        MagicMock(),
        paper_id=5,
        db_pool=pool,
    )

    assert result1 == {"status": "ok", "paper_id": 5}
    assert result2 == {"status": "ok", "paper_id": 5}
    # ON CONFLICT DO UPDATE means no error on repeat; execute called twice
    assert conn.execute.await_count == 2


@pytest.mark.asyncio
async def test_put_paper_bookmark_404_for_missing_paper():
    """bookmark_paper raises 404 when the paper does not exist."""
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = None
    conn.fetchval = AsyncMock(return_value=None)

    with pytest.raises(HTTPException, match="Paper not found") as exc_info:
        await papers.bookmark_paper.__wrapped__(
            MagicMock(),
            paper_id=999,
            db_pool=pool,
        )

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_bookmark_paper_toggles_between_starred_and_read():
    """H15-schema: bookmark_paper toggles starred ↔ read on successive calls.

    First call (no prior state) → status becomes 'starred'.
    Second call (prior status = 'starred') → status becomes 'read'.
    """
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = {"id": 5}

    # --- First call: no prior state → should star ---
    conn.fetchval = AsyncMock(return_value=None)
    result1 = await papers.bookmark_paper.__wrapped__(
        MagicMock(),
        paper_id=5,
        db_pool=pool,
    )
    assert result1 == {"status": "ok", "paper_id": 5}
    first_args = conn.execute.await_args.args
    assert "starred" in first_args, f"Expected 'starred' in execute args, got: {first_args}"

    # --- Second call: prior state = 'starred' → should set to 'read' ---
    conn.execute.reset_mock()
    conn.fetchval = AsyncMock(return_value="starred")
    result2 = await papers.bookmark_paper.__wrapped__(
        MagicMock(),
        paper_id=5,
        db_pool=pool,
    )
    assert result2 == {"status": "ok", "paper_id": 5}
    second_args = conn.execute.await_args.args
    assert "read" in second_args, (
        f"Expected 'read' in execute args after toggle from 'starred', got: {second_args}"
    )


@pytest.mark.asyncio
async def test_list_papers_no_filters_uses_limit_offset():
    """list_papers with no filters should still pass LIMIT/OFFSET as positional params."""
    pool, conn = _make_pool_and_conn()
    conn.fetch.return_value = [_paper_row()]

    await papers.list_papers.__wrapped__(
        SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None))),
        status=None,
        source_type=None,
        topic_id=None,
        q=None,
        limit=5,
        offset=10,
        db_pool=pool,
        embedder=None,
    )

    fetch_call = conn.fetch.await_args
    sql, *positional = fetch_call.args
    assert "LIMIT $1" in sql
    assert "OFFSET $2" in sql
    assert positional == [5, 10]


@pytest.mark.asyncio
async def test_list_papers_topic_filter_correct_param_indices():
    """list_papers with topic_id should use $1 for topic_id, $2/$3 for LIMIT/OFFSET."""
    pool, conn = _make_pool_and_conn()
    conn.fetch.return_value = [_paper_row()]

    await papers.list_papers.__wrapped__(
        SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None))),
        status=None,
        source_type=None,
        topic_id=42,
        q=None,
        limit=20,
        offset=0,
        db_pool=pool,
        embedder=None,
    )

    fetch_call = conn.fetch.await_args
    sql, *positional = fetch_call.args
    assert "pt.topic_id = $1" in sql
    assert "LIMIT $2" in sql
    assert "OFFSET $3" in sql
    assert positional == [42, 20, 0]


@pytest.mark.asyncio
async def test_list_papers_status_and_source_type_correct_param_indices():
    """list_papers with status + source_type assigns $1/$2 filters, $3/$4 LIMIT/OFFSET."""
    pool, conn = _make_pool_and_conn()
    conn.fetch.return_value = [_paper_row()]

    await papers.list_papers.__wrapped__(
        SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None))),
        status=PaperStatus.READ,
        source_type=SourceType.ARXIV,
        topic_id=None,
        q=None,
        limit=10,
        offset=5,
        db_pool=pool,
        embedder=None,
    )

    fetch_call = conn.fetch.await_args
    sql, *positional = fetch_call.args
    assert "pus.status = $1" in sql
    assert "p.source_type = $2" in sql
    assert "LIMIT $3" in sql
    assert "OFFSET $4" in sql
    assert positional == ["read", "arxiv", 10, 5]


@pytest.mark.asyncio
async def test_list_papers_all_filters_correct_param_indices():
    """list_papers with topic_id + status + source_type + q assigns params in order."""
    pool, conn = _make_pool_and_conn()
    conn.fetch.return_value = [_paper_row()]

    await papers.list_papers.__wrapped__(
        SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None))),
        status=PaperStatus.READ,
        source_type=SourceType.ARXIV,
        topic_id=7,
        q="neural",
        limit=15,
        offset=3,
        db_pool=pool,
        embedder=None,
    )

    fetch_call = conn.fetch.await_args
    sql, *positional = fetch_call.args
    # topic=$1, status=$2, source_type=$3, q=$4, LIMIT=$5, OFFSET=$6
    assert "pt.topic_id = $1" in sql
    assert "pus.status = $2" in sql
    assert "p.source_type = $3" in sql
    assert "plainto_tsquery" in sql and "$4" in sql
    assert "LIMIT $5" in sql
    assert "OFFSET $6" in sql
    assert positional == [7, "read", "arxiv", "neural", 15, 3]


# ---------------------------------------------------------------------------
# WS-6B-α — multi-user ownership wiring on paper-ID endpoints.
# Single-user mode is exercised by every other test (user_id=None bypass).
# These tests force a multi-user caller via monkeypatch on the router-local
# ``current_user_id_or_none`` symbol to confirm 403/200 behavior.
# ---------------------------------------------------------------------------


async def _async_user_99(_request):
    return 99


@pytest.mark.asyncio
async def test_get_paper_detail_403_for_other_user(monkeypatch):
    """WS-6B-α: paper owned by user 42, caller is user 99 → 403 from helper."""
    monkeypatch.setattr("paper_ingestion.routers.papers.current_user_id_or_none", _async_user_99)
    pool, conn = _make_pool_and_conn()
    # First fetchrow is the ownership check on `papers` table.
    conn.fetchrow.return_value = FakeRecord(user_id=42)

    with pytest.raises(HTTPException) as exc_info:
        await papers.get_paper_detail.__wrapped__(
            MagicMock(),
            paper_id=1,
            db_pool=pool,
        )
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_bookmark_paper_200_for_owner_match(monkeypatch):
    """WS-6B-α: paper owned by user 99, caller is also 99 → bookmark succeeds."""
    monkeypatch.setattr("paper_ingestion.routers.papers.current_user_id_or_none", _async_user_99)
    pool, conn = _make_pool_and_conn()
    # fetchrow #1 = ownership check (matching owner), #2 = paper-exists check.
    conn.fetchrow.side_effect = [FakeRecord(user_id=99), {"id": 5}]

    result = await papers.bookmark_paper.__wrapped__(
        MagicMock(),
        paper_id=5,
        db_pool=pool,
    )
    assert result == {"status": "ok", "paper_id": 5}
