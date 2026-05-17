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
    AnnotationsRequest,
    BulkActionRequest,
    Confidence,
    CrossReference,
    FeedbackRequest,
    KeyFinding,
    PaperCreate,
    PaperResponse,
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
    """Create a mock pool whose acquire() returns an async context manager.

    The returned connection's ``transaction()`` is also stubbed to behave as
    an async context manager so handlers that wrap SQL in ``async with
    conn.transaction()`` keep working without further ceremony.
    """
    conn = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx
    # conn.transaction() must work as an async context manager too.
    txn_ctx = MagicMock()
    txn_ctx.__aenter__ = AsyncMock(return_value=None)
    txn_ctx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn_ctx)
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


# ---------------------------------------------------------------------------
# list_papers — view-based filtering (post Phase A redesign)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_papers_with_view_inbox_uses_state_predicate():
    """``view='inbox'`` should LEFT JOIN paper_user_state and apply the
    COALESCE-based state predicate from VIEW_PREDICATES['inbox']."""
    pool, conn = _make_pool_and_conn()
    conn.fetch.return_value = [_paper_row()]

    rows = await papers.list_papers.__wrapped__(
        SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None))),
        view="inbox",
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
    assert "COALESCE(pus.state, 'inbox') = 'inbox'" in sql


@pytest.mark.asyncio
async def test_list_papers_unknown_view_raises_422():
    """An unrecognised ``view`` value should be rejected before SQL is built."""
    pool = _make_pool_and_conn()[0]

    with pytest.raises(HTTPException) as exc_info:
        await papers.list_papers.__wrapped__(
            SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None))),
            view="not-a-real-view",
            source_type=None,
            topic_id=None,
            q=None,
            limit=20,
            offset=0,
            db_pool=pool,
        )
    assert exc_info.value.status_code == 422


@pytest.mark.asyncio
async def test_list_papers_search_query_uses_bm25_clause():
    """list_papers should add the search_vector clause on BM25 fallback queries."""
    pool, conn = _make_pool_and_conn()
    conn.fetch.return_value = [_paper_row()]

    rows = await papers.list_papers.__wrapped__(
        SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None))),
        view=None,
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


# ---------------------------------------------------------------------------
# get_paper_detail — surfaces user_state + recent_feedback (post-redesign)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_paper_detail_raises_404_when_missing(monkeypatch):
    """get_paper_detail returns 404 when the paper row is absent.

    Ownership is covered by dedicated tests; here it is a pass-through so the
    route's own missing-paper 404 is exercised.
    """
    monkeypatch.setattr(
        "paper_ingestion.routers.papers.assert_paper_ownership", AsyncMock(return_value=None)
    )
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
    """get_paper_detail should compose the redesigned nested response payload.

    Per spec §9.1, ``user_state`` carries the new shape (state /
    state_before_trash / starred / rating / user_notes / flagged /
    updated_at) and the response also includes ``recent_feedback``.
    """
    pool, conn = _make_pool_and_conn()
    user_state_row = FakeRecord(
        state="reading",
        state_before_trash=None,
        starred=False,
        rating=4,
        user_notes="my notes",
        flagged=False,
        updated_at=datetime.now(UTC),
    )
    conn.fetchrow.side_effect = [
        _paper_row(id=3),  # SELECT * FROM papers
        {"id": 10},  # SELECT * FROM paper_summaries
        user_state_row,  # SELECT ... FROM paper_user_state
        None,  # SELECT signal,source,created_at FROM recommendation_feedback (no rows)
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
        patch.object(papers, "assert_paper_ownership", AsyncMock(return_value=None)),
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
    assert result.user_state is not None
    assert result.user_state.state == "reading"
    assert result.user_state.rating == 4
    assert result.user_state.user_notes == "my notes"
    assert result.user_state.flagged is False
    assert result.recent_feedback is None
    assert result.has_project_links is True
    # No failed paper.process/analyze job → left Pipeline rail stays non-failed.
    assert result.processing_failed is False
    paper_conv.assert_called_once()
    summary_conv.assert_called_once()
    chunk_conv.assert_called_once()
    # Two fetchvals now: project-link count + last-process-job status.
    assert conn.fetchval.await_count == 2


@pytest.mark.asyncio
async def test_get_paper_detail_starred_independent_of_state():
    """A starred paper in state='inbox' must surface as state='inbox' AND
    starred=True — they are orthogonal post-redesign (no collapse)."""
    pool, conn = _make_pool_and_conn()
    user_state_row = FakeRecord(
        state="inbox",
        state_before_trash=None,
        starred=True,
        rating=None,
        user_notes=None,
        flagged=False,
        updated_at=datetime.now(UTC),
    )
    conn.fetchrow.side_effect = [
        _paper_row(id=42),
        None,  # no summary
        user_state_row,
        None,  # no feedback
    ]
    conn.fetch.return_value = []
    conn.fetchval = AsyncMock(return_value=0)

    paper_model = PaperResponse(
        id=42,
        external_id="paper-42",
        source_type=SourceType.ARXIV,
        title="Paper 42",
        authors=["Ada"],
        url="https://example.com/papers/42",
        created_at=datetime.now(UTC),
    )

    with (
        patch.object(papers, "assert_paper_ownership", AsyncMock(return_value=None)),
        patch.object(papers, "row_to_paper_response", return_value=paper_model),
    ):
        result = await papers.get_paper_detail.__wrapped__(
            MagicMock(),
            paper_id=42,
            db_pool=pool,
        )

    assert result.user_state is not None
    assert result.user_state.state == "inbox"
    assert result.user_state.starred is True


@pytest.mark.asyncio
async def test_get_paper_detail_processing_failed_true_when_last_job_failed():
    """When the latest paper.process/paper.analyze job for this paper+user
    terminated in `failed`, the response carries processing_failed=True so the
    left Pipeline rail (PaperTOC) shows ✗ from the SAME persisted signal
    ActionsSidebar polls via getJob — no parallel status."""
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.side_effect = [
        _paper_row(id=7),
        None,  # no summary
        None,  # no user_state
        None,  # no feedback
    ]
    conn.fetch.return_value = []
    # fetchval order: (1) project-link COUNT → 0, (2) last process-job status.
    conn.fetchval = AsyncMock(side_effect=[0, "failed"])

    paper_model = PaperResponse(
        id=7,
        external_id="paper-7",
        source_type=SourceType.ARXIV,
        title="Paper 7",
        authors=["Ada"],
        url="https://example.com/papers/7",
        created_at=datetime.now(UTC),
    )

    with (
        patch.object(papers, "assert_paper_ownership", AsyncMock(return_value=None)),
        patch.object(papers, "row_to_paper_response", return_value=paper_model),
    ):
        result = await papers.get_paper_detail.__wrapped__(
            MagicMock(),
            paper_id=7,
            db_pool=pool,
        )

    assert result.processing_failed is True
    # The status query targets the same procrastinate_jobs source jobs polling
    # uses (task_name IN paper.process/paper.analyze).
    status_sql = conn.fetchval.await_args_list[1].args[0]
    assert "procrastinate_jobs" in status_sql
    assert "paper.process" in status_sql
    assert "paper.analyze" in status_sql


@pytest.mark.asyncio
async def test_get_paper_detail_processing_failed_false_when_last_job_succeeded():
    """A non-failed latest job leaves processing_failed=False."""
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.side_effect = [_paper_row(id=8), None, None, None]
    conn.fetch.return_value = []
    conn.fetchval = AsyncMock(side_effect=[0, "other"])

    paper_model = PaperResponse(
        id=8,
        external_id="paper-8",
        source_type=SourceType.ARXIV,
        title="Paper 8",
        authors=["Ada"],
        url="https://example.com/papers/8",
        created_at=datetime.now(UTC),
    )

    with (
        patch.object(papers, "assert_paper_ownership", AsyncMock(return_value=None)),
        patch.object(papers, "row_to_paper_response", return_value=paper_model),
    ):
        result = await papers.get_paper_detail.__wrapped__(
            MagicMock(),
            paper_id=8,
            db_pool=pool,
        )

    assert result.processing_failed is False


@pytest.mark.asyncio
async def test_get_paper_detail_sets_has_project_links_false_when_unlinked():
    """get_paper_detail should expose a false project-link flag when count is zero."""
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.side_effect = [
        _paper_row(id=4),
        None,
        None,
        None,  # no recommendation_feedback row
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

    with (
        patch.object(papers, "assert_paper_ownership", AsyncMock(return_value=None)),
        patch.object(papers, "row_to_paper_response", return_value=paper_model),
    ):
        result = await papers.get_paper_detail.__wrapped__(
            MagicMock(),
            paper_id=4,
            db_pool=pool,
        )

    assert result.has_project_links is False
    assert result.processing_failed is False
    # Two fetchvals now: project-link count + last-process-job status.
    assert conn.fetchval.await_count == 2


# ---------------------------------------------------------------------------
# batch_save_papers — unchanged from the legacy contract
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# submit_feedback — post-redesign FeedbackRequest validation + 404 mapping
# ---------------------------------------------------------------------------


def test_submit_feedback_validates_signal_and_source():
    """FeedbackRequest is the validation surface for POST /feedback.

    Pydantic raises before the handler runs in the real request path; the
    direct unit-test here exercises the model itself. Empty body, missing
    fields, and unknown literals all fail validation.
    """
    from pydantic import ValidationError

    # No fields at all → both required.
    with pytest.raises(ValidationError):
        FeedbackRequest.model_validate({})
    # signal alone is not enough — source is required.
    with pytest.raises(ValidationError):
        FeedbackRequest.model_validate({"signal": "positive"})
    # Unknown signal literal.
    with pytest.raises(ValidationError):
        FeedbackRequest.model_validate({"signal": "unknown", "source": "pulse_thumbs"})
    # Unknown source literal (rss_feed is not in the spec'd set).
    with pytest.raises(ValidationError):
        FeedbackRequest.model_validate({"signal": "positive", "source": "rss_feed"})


@pytest.mark.asyncio
async def test_submit_feedback_maps_foreign_key_violation_to_404():
    """submit_feedback should convert a FK error on the recommendation_feedback
    INSERT into a stable 404.

    After the DRY refactor, the error surfaces from conn.execute inside
    _upsert_recommendation_feedback (not conn.fetchrow).
    Feedback validation checks the live discovery_origin enum before the insert.
    """
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = FakeRecord(discovery_origin="pulse")
    conn.execute.side_effect = asyncpg.ForeignKeyViolationError("missing paper")
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None)))

    with pytest.raises(HTTPException, match="Paper 7 not found") as exc_info:
        await papers.submit_feedback.__wrapped__(
            request,
            paper_id=7,
            body=FeedbackRequest(signal="positive", source="feed_thumbs"),
            db_pool=pool,
        )

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_submit_feedback_writes_recommendation_feedback_with_correct_source():
    """submit_feedback delegates to _upsert_recommendation_feedback with the correct args.

    After the DRY refactor the raw inline INSERT was replaced by a call to
    the shared helper.  We patch the helper at the import path used by papers.py
    and verify it is called with paper_id, signal, source, and reason.
    Feedback validation checks the live discovery_origin enum before the insert.
    """
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = FakeRecord(discovery_origin="recommender")
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None)))

    with patch(
        "paper_ingestion.routers.papers._upsert_recommendation_feedback",
        new_callable=AsyncMock,
    ) as mock_helper:
        result = await papers.submit_feedback.__wrapped__(
            request,
            paper_id=7,
            body=FeedbackRequest(signal="negative", source="feed_thumbs", reason="off-topic"),
            db_pool=pool,
        )

    mock_helper.assert_awaited_once()
    assert mock_helper.await_args is not None
    _conn_arg, paper_id_arg, _uid_arg, signal_arg, source_arg, reason_arg = (
        mock_helper.await_args.args
    )
    assert paper_id_arg == 7
    assert signal_arg == "negative"
    assert source_arg == "feed_thumbs"
    assert reason_arg == "off-topic"
    assert result.signal == "negative"
    assert result.source == "feed_thumbs"


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["pulse", "recommender", "citation_batch"])
async def test_submit_feedback_accepts_system_discovered_origins(origin: str):
    """Explicit thumbs are allowed for all current system-discovered origins."""
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = FakeRecord(discovery_origin=origin)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None)))

    with patch(
        "paper_ingestion.routers.papers._upsert_recommendation_feedback",
        new_callable=AsyncMock,
    ) as mock_helper:
        result = await papers.submit_feedback.__wrapped__(
            request,
            paper_id=7,
            body=FeedbackRequest(signal="positive", source="feed_thumbs"),
            db_pool=pool,
        )

    mock_helper.assert_awaited_once()
    assert result.source == "feed_thumbs"


@pytest.mark.asyncio
async def test_submit_feedback_rejects_user_initiated_papers():
    """User-initiated papers are excluded from recommendation feedback/training."""
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = FakeRecord(discovery_origin="user_initiated")
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None)))

    with pytest.raises(HTTPException) as exc_info:
        await papers.submit_feedback.__wrapped__(
            request,
            paper_id=7,
            body=FeedbackRequest(signal="positive", source="paper_detail_thumbs"),
            db_pool=pool,
        )

    assert exc_info.value.status_code == 400
    assert "user_initiated" in exc_info.value.detail
    conn.execute.assert_not_awaited()


# ---------------------------------------------------------------------------
# list_papers — positional parameter wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_papers_no_filters_uses_limit_offset():
    """list_papers with no filters scopes to user_library then LIMIT/OFFSET.

    RB-1: user_id is unconditionally bound ($1) so library scoping fires even
    when no view/topic/source/q filters are active.
    """
    pool, conn = _make_pool_and_conn()
    conn.fetch.return_value = [_paper_row()]

    await papers.list_papers.__wrapped__(
        SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None))),
        view=None,
        source_type=None,
        topic_id=None,
        q=None,
        limit=5,
        offset=10,
        db_pool=pool,
        embedder=None,
        user_id=1,
    )

    fetch_call = conn.fetch.await_args
    sql, *positional = fetch_call.args
    assert "JOIN user_library ul" in sql
    assert "ul.user_id = $1" in sql
    assert "LIMIT $2" in sql
    assert "OFFSET $3" in sql
    assert positional == [1, 5, 10]


@pytest.mark.asyncio
async def test_list_papers_topic_filter_correct_param_indices():
    """list_papers with topic_id: $1=topic_id, $2=user_id (library join), $3/$4 LIMIT/OFFSET.

    RB-1: user_library join is inserted after topic_id in param order.
    """
    pool, conn = _make_pool_and_conn()
    conn.fetch.return_value = [_paper_row()]

    await papers.list_papers.__wrapped__(
        SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None))),
        view=None,
        source_type=None,
        topic_id=42,
        q=None,
        limit=20,
        offset=0,
        db_pool=pool,
        embedder=None,
        user_id=1,
    )

    fetch_call = conn.fetch.await_args
    sql, *positional = fetch_call.args
    assert "pt.topic_id = $1" in sql
    assert "JOIN user_library ul" in sql
    assert "ul.user_id = $2" in sql
    assert "LIMIT $3" in sql
    assert "OFFSET $4" in sql
    assert positional == [42, 1, 20, 0]


@pytest.mark.asyncio
async def test_list_papers_view_and_source_type_correct_param_indices():
    """list_papers with view + source_type:
    $1=user_id (library join), $2=user_id (pus LEFT JOIN), $3=source_type,
    $4/$5 LIMIT/OFFSET.

    RB-1: user_library join is unconditionally added before the view's
    paper_user_state join, so user_id is bound twice (once per join).
    ``view`` itself is a literal predicate from VIEW_PREDICATES — no extra param.
    """
    pool, conn = _make_pool_and_conn()
    conn.fetch.return_value = [_paper_row()]

    await papers.list_papers.__wrapped__(
        SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None))),
        view="reading",
        source_type=SourceType.ARXIV,
        topic_id=None,
        q=None,
        limit=10,
        offset=5,
        db_pool=pool,
        embedder=None,
        user_id=1,
    )

    fetch_call = conn.fetch.await_args
    sql, *positional = fetch_call.args
    assert "IS NOT DISTINCT FROM" not in sql
    assert "JOIN user_library ul" in sql
    assert "ul.user_id = $1" in sql
    assert "LEFT JOIN paper_user_state pus" in sql
    assert "pus.user_id = $2" in sql
    assert "p.source_type = $3" in sql
    assert "LIMIT $4" in sql
    assert "OFFSET $5" in sql
    assert "COALESCE(pus.state, 'inbox') = 'reading'" in sql
    assert positional == [1, 1, "arxiv", 10, 5]


@pytest.mark.asyncio
async def test_list_papers_all_filters_correct_param_indices():
    """list_papers with topic_id + view + source_type + q binds in order:

    $1=topic_id, $2=user_id (library join), $3=user_id (pus LEFT JOIN),
    $4=source_type.value, $5=q, $6=LIMIT, $7=OFFSET.
    ``view`` is a literal predicate from VIEW_PREDICATES — no extra param.

    RB-1: user_library join ($2) is added unconditionally between topic_id
    and the view's paper_user_state join ($3).
    """
    pool, conn = _make_pool_and_conn()
    conn.fetch.return_value = [_paper_row()]

    await papers.list_papers.__wrapped__(
        SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None))),
        view="reading",
        source_type=SourceType.ARXIV,
        topic_id=7,
        q="neural",
        limit=15,
        offset=3,
        db_pool=pool,
        embedder=None,
        user_id=1,
    )

    fetch_call = conn.fetch.await_args
    sql, *positional = fetch_call.args
    assert "pt.topic_id = $1" in sql
    assert "IS NOT DISTINCT FROM" not in sql
    assert "JOIN user_library ul" in sql
    assert "ul.user_id = $2" in sql
    assert "pus.user_id = $3" in sql
    assert "p.source_type = $4" in sql
    assert "plainto_tsquery" in sql and "$5" in sql
    assert "LIMIT $6" in sql
    assert "OFFSET $7" in sql
    assert positional == [7, 1, 1, "arxiv", "neural", 15, 3]


# ---------------------------------------------------------------------------
# RB-1 — BM25/fallback list_papers scoped to caller's user_library
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_papers_bm25_scoped_to_user(monkeypatch):
    """RB-1: user B calling the BM25 path (q set, view=None) must NOT receive
    user A's papers.

    The query SQL must include a JOIN on user_library bound to the caller's
    user_id, not user A's.  We simulate two calls — one as user A (id=1, the
    autouse default) and one as user B (id=2) — and verify that:

    1. Both calls contain ``JOIN user_library ul`` in the SQL.
    2. Each call's user_library param matches the caller's own id (not the
       other user's id), proving cross-tenant rows are structurally excluded.
    """
    # --- call as user A (id=1, the autouse default) ---
    pool_a, conn_a = _make_pool_and_conn()
    conn_a.fetch.return_value = [_paper_row(id=10)]

    # CC-03: identity is now a Depends(get_current_user_id) param; a direct
    # .__wrapped__ call passes it explicitly. user A == id 1 (the value the
    # pre-conversion autouse stub supplied).
    await papers.list_papers.__wrapped__(
        SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None))),
        view=None,
        source_type=None,
        topic_id=None,
        q="neural",
        limit=10,
        offset=0,
        db_pool=pool_a,
        embedder=None,
        user_id=1,
    )

    sql_a, *params_a = conn_a.fetch.await_args.args
    assert "JOIN user_library ul" in sql_a, "user_library join must be present for user A"
    # user_id=1 (user A) must be the param bound to the library join
    assert params_a[0] == 1, f"Expected user_id=1 for user A, got {params_a[0]}"

    # --- call as user B (id=2) — pass the distinct caller identity directly ---
    pool_b, conn_b = _make_pool_and_conn()
    conn_b.fetch.return_value = []  # user B has no papers — cross-tenant leak would add rows

    await papers.list_papers.__wrapped__(
        SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None))),
        view=None,
        source_type=None,
        topic_id=None,
        q="neural",
        limit=10,
        offset=0,
        db_pool=pool_b,
        embedder=None,
        user_id=2,
    )

    sql_b, *params_b = conn_b.fetch.await_args.args
    assert "JOIN user_library ul" in sql_b, "user_library join must be present for user B"
    # user_id=2 (user B) must be bound — NOT user A's id (1)
    assert params_b[0] == 2, f"Expected user_id=2 for user B, got {params_b[0]}"
    assert params_b[0] != 1, "user B must not be scoped to user A's library (cross-tenant leak)"


# ---------------------------------------------------------------------------
# WS-6B-α — multi-user ownership wiring on paper-ID endpoints.
# Single-user mode is exercised by every other test (user_id=None bypass).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_paper_detail_403_for_other_user():
    """Sprint B: paper discovered_by=42, caller=99 + not in library → 403."""
    pool, conn = _make_pool_and_conn()
    # First fetchrow is the ownership check on `papers` table — return the
    # legacy ``user_id`` key (the helper falls back to it when
    # ``discovered_by`` is missing).
    conn.fetchrow.return_value = FakeRecord(user_id=42)
    # Sprint B: assert_paper_ownership now also probes user_library via
    # fetchval; force a "not in library" miss so the 403 fires.
    conn.fetchval = AsyncMock(return_value=None)

    with pytest.raises(HTTPException) as exc_info:
        await papers.get_paper_detail.__wrapped__(
            MagicMock(),
            paper_id=1,
            db_pool=pool,
            user_id=99,
        )
    assert exc_info.value.status_code == 403


# ---------------------------------------------------------------------------
# Single-paper lifecycle endpoints — Phase A Wave 1ab (state mutators)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_paper_sets_state_to_read():
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = FakeRecord(id=42)  # paper-exists check
    conn.fetchval.return_value = "inbox"  # _assert_paper_in_states precondition (W1.7-B)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None)))

    result = await papers.save_paper.__wrapped__(request, 42, db_pool=pool)

    assert result == {"status": "ok", "paper_id": 42}
    sql_calls = [call.args[0] for call in conn.execute.await_args_list]
    assert any("INSERT INTO paper_user_state" in sql and "state" in sql for sql in sql_calls), (
        f"Expected an INSERT writing state; got: {sql_calls}"
    )
    # The state value 'to_read' is bound positionally — assert via execute args.
    assert any(
        "to_read" in [str(arg) for arg in call.args] for call in conn.execute.await_args_list
    ), f"Expected 'to_read' in execute args; got: {conn.execute.await_args_list!r}"


@pytest.mark.asyncio
async def test_skip_paper_sets_state_done():
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = FakeRecord(id=42)
    conn.fetchval.return_value = "inbox"  # _assert_paper_in_states precondition (W1.7-B)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None)))

    result = await papers.skip_paper.__wrapped__(request, 42, db_pool=pool)

    assert result == {"status": "ok", "paper_id": 42}
    assert any("done" in [str(arg) for arg in call.args] for call in conn.execute.await_args_list)


@pytest.mark.asyncio
async def test_reading_paper_sets_state_reading():
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = FakeRecord(id=42)
    conn.fetchval.return_value = "to_read"  # _assert_paper_in_states precondition (W1.7-B)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None)))

    result = await papers.reading_paper.__wrapped__(request, 42, db_pool=pool)

    assert result == {"status": "ok", "paper_id": 42}
    assert any(
        "reading" in [str(arg) for arg in call.args] for call in conn.execute.await_args_list
    )


@pytest.mark.asyncio
async def test_done_paper_sets_state_done():
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = FakeRecord(id=42)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None)))

    result = await papers.done_paper.__wrapped__(request, 42, db_pool=pool)

    assert result == {"status": "ok", "paper_id": 42}
    assert any("done" in [str(arg) for arg in call.args] for call in conn.execute.await_args_list)


@pytest.mark.asyncio
async def test_star_paper_sets_starred_true_does_not_change_state():
    """``star`` writes ``starred = $N`` only — no ``state =`` clause.

    Group B rewrote star_paper to use a CTE + RETURNING fetchrow (not execute)
    so the upsert SQL is now in conn.fetchrow calls, not conn.execute.
    """
    pool, conn = _make_pool_and_conn()
    # First fetchrow: paper-existence check returns the paper row.
    # Second fetchrow: CTE RETURNING — returns is_new_row + prev_starred.
    conn.fetchrow.side_effect = [
        FakeRecord(id=42),
        FakeRecord(is_new_row=True, prev_starred=False),
    ]
    # fetchval: COUNT(*) from project_papers → 0 (no zotero.push needed)
    conn.fetchval.return_value = 0
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None)))

    with patch.object(papers, "assert_paper_ownership", AsyncMock(return_value=None)):
        result = await papers.star_paper.__wrapped__(request, 42, db_pool=pool)

    assert result == {"status": "ok", "paper_id": 42}
    # Group B: upsert is now via conn.fetchrow (CTE WITH RETURNING), not conn.execute.
    fetchrow_sql_calls = [call.args[0] for call in conn.fetchrow.await_args_list]
    upsert_sql = next(
        (sql for sql in fetchrow_sql_calls if "INSERT INTO paper_user_state" in sql), None
    )
    assert upsert_sql is not None, (
        f"Expected CTE upsert in fetchrow calls; got {fetchrow_sql_calls}"
    )
    assert "starred" in upsert_sql
    # The DO UPDATE SET clause must NOT touch state.
    do_update_clause = upsert_sql.split("DO UPDATE SET", 1)[-1]
    assert "state =" not in do_update_clause, (
        f"star_paper must not write state; DO UPDATE SET = {do_update_clause!r}"
    )


@pytest.mark.asyncio
async def test_unstar_paper_sets_starred_false():
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = FakeRecord(id=42)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None)))

    result = await papers.unstar_paper.__wrapped__(request, 42, db_pool=pool)

    assert result == {"status": "ok", "paper_id": 42}
    sql_calls = [call.args[0] for call in conn.execute.await_args_list]
    upsert_sql = next((sql for sql in sql_calls if "INSERT INTO paper_user_state" in sql), None)
    assert upsert_sql is not None
    assert "starred" in upsert_sql
    assert any(False in call.args for call in conn.execute.await_args_list)


@pytest.mark.asyncio
async def test_trash_paper_sets_state_trash_and_records_state_before_trash():
    """The atomic UPDATE branch sets state_before_trash via a CASE expression
    (preserves prior value on re-trash, otherwise records current state) while
    writing state := 'trash' in a single statement — no read-then-write race."""
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = FakeRecord(id=42)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None)))

    result = await papers.trash_paper.__wrapped__(request, 42, db_pool=pool)

    assert result == {"status": "ok", "paper_id": 42}
    sql_calls = [call.args[0] for call in conn.execute.await_args_list]
    trash_sql = next(
        (sql for sql in sql_calls if "state_before_trash" in sql and "'trash'" in sql),
        None,
    )
    assert trash_sql is not None, f"Expected atomic trash SQL; got {sql_calls}"
    # W1.7-B: re-trash guard via CASE expression — preserves prior
    # state_before_trash when already in 'trash'; otherwise records current state.
    assert "CASE" in trash_sql
    assert (
        "WHEN paper_user_state.state = 'trash' THEN paper_user_state.state_before_trash"
        in trash_sql
    )
    assert "ELSE paper_user_state.state" in trash_sql
    assert "state = 'trash'" in trash_sql


@pytest.mark.asyncio
async def test_restore_paper_returns_state_before_trash_to_state():
    """Restore: ``state := COALESCE(state_before_trash, 'inbox')`` and
    ``state_before_trash := NULL`` — the COALESCE handles the
    null-state_before_trash case implicitly (defensive default).
    Group B: _restore_paper checks asyncpg's status string ("UPDATE N") to
    detect 0-row updates; conn.execute.return_value must be "UPDATE 1".
    """
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = FakeRecord(id=42)
    conn.fetchval.return_value = "trash"  # _assert_paper_in_state: paper is in trash
    # Group B: _restore_paper parses conn.execute return value as "UPDATE N"
    conn.execute.return_value = "UPDATE 1"
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None)))

    result = await papers.restore_paper.__wrapped__(request, 42, db_pool=pool)

    assert result == {"status": "ok", "paper_id": 42}
    sql_calls = [call.args[0] for call in conn.execute.await_args_list]
    restore_sql = next(
        (
            sql
            for sql in sql_calls
            if "COALESCE(state_before_trash, 'inbox')" in sql and "state_before_trash = NULL" in sql
        ),
        None,
    )
    assert restore_sql is not None, f"Expected restore SQL with COALESCE; got {sql_calls}"


@pytest.mark.asyncio
async def test_restore_paper_non_trash_returns_409():
    """W1.2 precondition: restore on a non-trash paper must raise 409.

    A paper in 'inbox' (or any non-trash state) must not be silently demoted
    to inbox; the state machine requires the paper to be in 'trash' first.
    """
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = FakeRecord(id=42)
    conn.fetchval.return_value = "inbox"  # _assert_paper_in_state: NOT in trash
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None)))

    with pytest.raises(HTTPException) as exc_info:
        await papers.restore_paper.__wrapped__(request, 42, db_pool=pool)

    assert exc_info.value.status_code == 409
    # Confirm _restore_paper was NOT called (no COALESCE execute in sql_calls).
    sql_calls = [call.args[0] for call in conn.execute.await_args_list]
    assert not any("COALESCE(state_before_trash, 'inbox')" in sql for sql in sql_calls), (
        f"_restore_paper must not run when precondition fails; got {sql_calls}"
    )


@pytest.mark.asyncio
async def test_trash_and_reject_writes_both_lifecycle_and_feedback():
    """trash_and_reject is the only combined action (spec §4.4): one txn
    issues the atomic trash UPDATE *and* a recommendation_feedback row with
    signal='negative' / source='dismiss_combined'."""
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = FakeRecord(id=42)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None)))

    await papers.trash_and_reject_paper.__wrapped__(request, 42, db_pool=pool)

    sql_calls = [call.args[0] for call in conn.execute.await_args_list]
    trash_sql = next((sql for sql in sql_calls if "state = 'trash'" in sql), None)
    feedback_sql = next(
        (sql for sql in sql_calls if "INSERT INTO recommendation_feedback" in sql),
        None,
    )
    assert trash_sql is not None, f"Expected trash SQL; got {sql_calls}"
    assert feedback_sql is not None, f"Expected feedback SQL; got {sql_calls}"
    # Verify the feedback row had signal='negative' and source='dismiss_combined'.
    feedback_call = next(
        call
        for call in conn.execute.await_args_list
        if "INSERT INTO recommendation_feedback" in call.args[0]
    )
    assert "negative" in feedback_call.args
    assert "dismiss_combined" in feedback_call.args


# ---------------------------------------------------------------------------
# annotate_paper — partial upsert for rating / user_notes / flagged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_annotate_paper_writes_rating_user_notes_flagged():
    """annotate_paper upserts paper_user_state with all three annotation
    fields and projects the new shape (state / state_before_trash /
    starred / rating / user_notes / flagged / updated_at) via RETURNING."""
    pool, conn = _make_pool_and_conn()
    now = datetime.now(UTC)
    conn.fetchrow.return_value = FakeRecord(
        state="inbox",
        state_before_trash=None,
        starred=False,
        rating=4,
        user_notes="test",
        flagged=True,
        updated_at=now,
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None)))

    result = await papers.annotate_paper.__wrapped__(
        request,
        42,
        body=AnnotationsRequest(rating=4, user_notes="test", flagged=True),
        db_pool=pool,
    )

    assert result.rating == 4
    assert result.user_notes == "test"
    assert result.flagged is True
    sql = conn.fetchrow.await_args.args[0]
    assert "INSERT INTO paper_user_state" in sql
    assert "RETURNING" in sql
    # RETURNING projects the new shape per spec §9.1.
    for col in (
        "state",
        "state_before_trash",
        "starred",
        "rating",
        "user_notes",
        "flagged",
        "updated_at",
    ):
        assert col in sql, f"Expected column {col!r} in RETURNING clause"


@pytest.mark.asyncio
async def test_annotate_paper_partial_update_only_rating():
    """Partial update: rating set, user_notes/flagged omitted → COALESCE
    preservation via ``COALESCE($N, paper_user_state.<col>)``."""
    pool, conn = _make_pool_and_conn()
    now = datetime.now(UTC)
    conn.fetchrow.return_value = FakeRecord(
        state="inbox",
        state_before_trash=None,
        starred=False,
        rating=5,
        user_notes=None,
        flagged=False,
        updated_at=now,
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None)))

    result = await papers.annotate_paper.__wrapped__(
        request,
        42,
        body=AnnotationsRequest(rating=5),
        db_pool=pool,
    )

    assert result.rating == 5
    sql = conn.fetchrow.await_args.args[0]
    assert "COALESCE($4, paper_user_state.user_notes)" in sql
    assert "COALESCE($5, paper_user_state.flagged)" in sql


def test_annotate_paper_validates_rating_range():
    """rating > 5 fails Pydantic validation (ge=1, le=5)."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        AnnotationsRequest(rating=6)
    with pytest.raises(ValidationError):
        AnnotationsRequest(rating=0)


# ---------------------------------------------------------------------------
# Hard delete — WS-AH2 NEW-H2 regression triple (spec §13 row 10)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hard_delete_409_when_paper_not_in_trash():
    """Hard delete must refuse to operate on papers not in state='trash'."""
    pool, conn = _make_pool_and_conn()
    conn.fetchval = AsyncMock(return_value="reading")
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None)))

    with patch(
        "paper_ingestion.routers.papers.delete_paper_vectors",
        new_callable=AsyncMock,
    ) as mock_qdrant:
        with pytest.raises(HTTPException) as exc_info:
            await papers.hard_delete_paper.__wrapped__(request, 42, db_pool=pool)
    assert exc_info.value.status_code == 409
    assert "trash" in exc_info.value.detail
    mock_qdrant.assert_not_called()


@pytest.mark.asyncio
async def test_hard_delete_aborts_qdrant_when_sql_delete_fails():
    """WS-AH2 NEW-H2: SQL DELETE failure aborts before Qdrant is touched.

    If the inside-txn DELETE raises, the txn rolls back and the outside
    Qdrant cleanup must never run — otherwise we'd orphan Qdrant vectors
    while the row remains, the data-loss-prone direction.
    """
    pool, conn = _make_pool_and_conn()
    conn.fetchval = AsyncMock(return_value="trash")  # state precondition passes
    conn.execute.side_effect = asyncpg.PostgresError("FK violation")
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None)))

    with patch(
        "paper_ingestion.routers.papers.delete_paper_vectors",
        new_callable=AsyncMock,
    ) as mock_qdrant:
        with pytest.raises(asyncpg.PostgresError):
            await papers.hard_delete_paper.__wrapped__(request, 42, db_pool=pool)
        # CRITICAL: Qdrant must NOT be called when SQL DELETE failed.
        mock_qdrant.assert_not_called()


@pytest.mark.asyncio
async def test_hard_delete_logs_qdrant_failure_after_sql_success(caplog):
    """WS-AH2 NEW-H2: Qdrant failure after SQL commit is logged but does
    not propagate — orphan vectors are recoverable, but propagating would
    surface a 500 to a user whose row was already deleted."""
    import logging as _logging

    pool, conn = _make_pool_and_conn()
    conn.fetchval = AsyncMock(return_value="trash")
    conn.execute.return_value = None  # SQL DELETE succeeds.
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None)))

    with caplog.at_level(_logging.ERROR, logger="paper_ingestion.routers.papers"):
        with patch(
            "paper_ingestion.routers.papers.delete_paper_vectors",
            new_callable=AsyncMock,
            side_effect=RuntimeError("qdrant down"),
        ) as mock_qdrant:
            result = await papers.hard_delete_paper.__wrapped__(request, 42, db_pool=pool)

    assert result == {"deleted": 42}
    mock_qdrant.assert_called_once_with(42)
    # The exception is logged via logger.exception with a recognisable message.
    assert any("Qdrant cleanup failed" in r.message for r in caplog.records), (
        f"Expected 'Qdrant cleanup failed' in caplog records; got "
        f"{[r.message for r in caplog.records]!r}"
    )


@pytest.mark.asyncio
async def test_hard_delete_calls_qdrant_after_sql_succeeds():
    """WS-AH2 NEW-H2: the happy path — SQL DELETE commits, then Qdrant is
    called exactly once. The DELETE FROM papers SQL must appear before
    the Qdrant call returns."""
    pool, conn = _make_pool_and_conn()
    conn.fetchval = AsyncMock(return_value="trash")
    conn.execute.return_value = None
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None)))

    with patch(
        "paper_ingestion.routers.papers.delete_paper_vectors",
        new_callable=AsyncMock,
    ) as mock_qdrant:
        result = await papers.hard_delete_paper.__wrapped__(request, 42, db_pool=pool)

    assert result == {"deleted": 42}
    mock_qdrant.assert_called_once_with(42)
    # Verify SQL DELETE appears in the execute call sequence.
    sql_calls = [call.args[0] for call in conn.execute.await_args_list]
    assert any("DELETE FROM papers" in sql for sql in sql_calls), (
        f"Expected DELETE FROM papers; got {sql_calls}"
    )


# ---------------------------------------------------------------------------
# Bulk action — POST /api/papers/bulk (spec §4.5, 10-action enum)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_save_action_sets_state_to_read():
    pool, conn = _make_pool_and_conn()
    body = BulkActionRequest(paper_ids=[1, 2], action="save")
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None)))

    result = await papers.bulk_action_papers.__wrapped__(request, body, db_pool=pool)

    assert result == {"succeeded": [1, 2], "failed": []}
    sql_calls = [call.args[0] for call in conn.execute.await_args_list]
    assert any("INSERT INTO paper_user_state" in sql for sql in sql_calls)
    assert any(
        "to_read" in [str(arg) for arg in call.args] for call in conn.execute.await_args_list
    )


@pytest.mark.asyncio
async def test_bulk_skip_action_sets_state_done():
    pool, conn = _make_pool_and_conn()
    body = BulkActionRequest(paper_ids=[1], action="skip")
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None)))

    result = await papers.bulk_action_papers.__wrapped__(request, body, db_pool=pool)

    assert result["succeeded"] == [1]
    assert any("done" in [str(arg) for arg in call.args] for call in conn.execute.await_args_list)


@pytest.mark.asyncio
async def test_bulk_mark_reading_action_sets_state_reading():
    pool, conn = _make_pool_and_conn()
    body = BulkActionRequest(paper_ids=[1], action="mark_reading")
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None)))

    result = await papers.bulk_action_papers.__wrapped__(request, body, db_pool=pool)

    assert result["succeeded"] == [1]
    assert any(
        "reading" in [str(arg) for arg in call.args] for call in conn.execute.await_args_list
    )


@pytest.mark.asyncio
async def test_bulk_mark_done_action_sets_state_done():
    pool, conn = _make_pool_and_conn()
    body = BulkActionRequest(paper_ids=[1], action="mark_done")
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None)))

    result = await papers.bulk_action_papers.__wrapped__(request, body, db_pool=pool)

    assert result["succeeded"] == [1]
    assert any("done" in [str(arg) for arg in call.args] for call in conn.execute.await_args_list)


@pytest.mark.asyncio
async def test_bulk_restore_action_uses_coalesce_state_before_trash():
    pool, conn = _make_pool_and_conn()
    conn.fetchval.return_value = "trash"  # _assert_paper_in_state: paper is in trash
    # Group B: _restore_paper parses conn.execute return value as "UPDATE N"
    conn.execute.return_value = "UPDATE 1"
    body = BulkActionRequest(paper_ids=[1], action="restore")
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None)))

    result = await papers.bulk_action_papers.__wrapped__(request, body, db_pool=pool)

    assert result["succeeded"] == [1]
    sql_calls = [call.args[0] for call in conn.execute.await_args_list]
    assert any("COALESCE(state_before_trash, 'inbox')" in sql for sql in sql_calls), (
        f"Expected restore SQL pattern; got {sql_calls}"
    )


@pytest.mark.asyncio
async def test_bulk_restore_non_trash_papers_surfaces_failures():
    """W1.2 precondition (bulk): restore on non-trash papers records them in 'failed'.

    bulk_action_papers uses per-paper savepoints; a 409 from
    _assert_paper_in_state is caught and surfaced in the 'failed' list.
    The papers' states must remain unchanged (no COALESCE execute issued).
    """
    pool, conn = _make_pool_and_conn()
    conn.fetchval.return_value = "inbox"  # _assert_paper_in_state: NOT in trash
    body = BulkActionRequest(paper_ids=[10, 20], action="restore")
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None)))

    result = await papers.bulk_action_papers.__wrapped__(request, body, db_pool=pool)

    assert result["succeeded"] == [], f"No papers should succeed; got {result['succeeded']}"
    assert len(result["failed"]) == 2, f"Both papers should fail; got {result['failed']}"
    failed_ids = {entry["paper_id"] for entry in result["failed"]}
    assert failed_ids == {10, 20}
    # _restore_paper must NOT have run for any paper.
    sql_calls = [call.args[0] for call in conn.execute.await_args_list]
    assert not any("COALESCE(state_before_trash, 'inbox')" in sql for sql in sql_calls), (
        f"_restore_paper must not run when precondition fails; got {sql_calls}"
    )


@pytest.mark.asyncio
async def test_bulk_star_action_writes_starred_true_only():
    pool, conn = _make_pool_and_conn()
    body = BulkActionRequest(paper_ids=[1], action="star")
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None)))

    result = await papers.bulk_action_papers.__wrapped__(request, body, db_pool=pool)

    assert result["succeeded"] == [1]
    sql_calls = [call.args[0] for call in conn.execute.await_args_list]
    upsert_sql = next((sql for sql in sql_calls if "INSERT INTO paper_user_state" in sql), None)
    assert upsert_sql is not None
    assert "starred" in upsert_sql
    do_update_clause = upsert_sql.split("DO UPDATE SET", 1)[-1]
    assert "state =" not in do_update_clause


@pytest.mark.asyncio
async def test_bulk_unstar_action_writes_starred_false():
    pool, conn = _make_pool_and_conn()
    body = BulkActionRequest(paper_ids=[1], action="unstar")
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None)))

    result = await papers.bulk_action_papers.__wrapped__(request, body, db_pool=pool)

    assert result["succeeded"] == [1]
    sql_calls = [call.args[0] for call in conn.execute.await_args_list]
    assert any("INSERT INTO paper_user_state" in sql and "starred" in sql for sql in sql_calls)


@pytest.mark.asyncio
async def test_bulk_trash_action_sets_state_before_trash_atomically():
    pool, conn = _make_pool_and_conn()
    body = BulkActionRequest(paper_ids=[1, 2], action="trash")
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None)))

    result = await papers.bulk_action_papers.__wrapped__(request, body, db_pool=pool)

    assert result == {"succeeded": [1, 2], "failed": []}
    sql_calls = [call.args[0] for call in conn.execute.await_args_list]
    # W1.7-B: re-trash CASE expression preserves prior state_before_trash
    # when already in 'trash'; otherwise records current state.
    assert any(
        "ELSE paper_user_state.state" in sql and "state = 'trash'" in sql for sql in sql_calls
    ), f"Expected atomic trash SQL with CASE expression; got {sql_calls}"


@pytest.mark.asyncio
async def test_bulk_feedback_positive_writes_recommendation_feedback():
    pool, conn = _make_pool_and_conn()
    body = BulkActionRequest(paper_ids=[1], action="feedback_positive")
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None)))

    result = await papers.bulk_action_papers.__wrapped__(request, body, db_pool=pool)

    assert result["succeeded"] == [1]
    sql_calls = [call.args[0] for call in conn.execute.await_args_list]
    feedback_sql = next(
        (sql for sql in sql_calls if "INSERT INTO recommendation_feedback" in sql),
        None,
    )
    assert feedback_sql is not None
    feedback_call = next(
        call
        for call in conn.execute.await_args_list
        if "INSERT INTO recommendation_feedback" in call.args[0]
    )
    assert "positive" in feedback_call.args
    assert "feed_thumbs" in feedback_call.args


@pytest.mark.asyncio
async def test_bulk_feedback_negative_writes_recommendation_feedback():
    pool, conn = _make_pool_and_conn()
    body = BulkActionRequest(paper_ids=[1], action="feedback_negative")
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None)))

    result = await papers.bulk_action_papers.__wrapped__(request, body, db_pool=pool)

    assert result["succeeded"] == [1]
    feedback_call = next(
        call
        for call in conn.execute.await_args_list
        if "INSERT INTO recommendation_feedback" in call.args[0]
    )
    assert "negative" in feedback_call.args
    assert "feed_thumbs" in feedback_call.args


@pytest.mark.asyncio
async def test_bulk_partial_failure_records_savepoint_isolation():
    """Per-paper savepoints must isolate failures: paper 2 raises, papers
    1 and 3 still succeed (the outer txn commits, only paper 2's
    savepoint rolls back)."""
    pool, conn = _make_pool_and_conn()

    # Sprint B: assert_paper_ownership now reads ``discovered_by`` (audit) +
    # may probe ``user_library`` membership via fetchval. Build a side_effect
    # that returns the legacy ``user_id`` key (fallback path) and mismatches
    # paper 2 to trigger a 403 (combined with a fetchval miss below).
    fetched: list[FakeRecord] = []

    async def _fetchrow(sql: str, *args, **kwargs):
        del kwargs  # unused but required by asyncpg.Connection.fetchrow signature
        if "FROM papers WHERE id" in sql or "SELECT discovered_by FROM papers" in sql:
            paper_id = args[0]
            if paper_id == 2:
                fetched.append(FakeRecord(user_id=999))
                return FakeRecord(user_id=999)  # mismatch → library probe + 403
            fetched.append(FakeRecord(user_id=99))
            return FakeRecord(user_id=99)
        return None

    async def _fetchval(sql: str, *args, **kwargs):
        del kwargs
        if "FROM user_library" in sql:
            paper_id = args[0]
            return None if paper_id == 2 else 1
        return None

    conn.fetchrow.side_effect = _fetchrow
    conn.fetchval = AsyncMock(side_effect=_fetchval)

    body = BulkActionRequest(paper_ids=[1, 2, 3], action="save")
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None)))

    result = await papers.bulk_action_papers.__wrapped__(request, body, db_pool=pool, user_id=99)

    assert result["succeeded"] == [1, 3]
    assert len(result["failed"]) == 1
    assert result["failed"][0]["paper_id"] == 2
    assert "error" in result["failed"][0]


# ---------------------------------------------------------------------------
# Idempotency + 404 tests — lifecycle state mutators (Task A.1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_paper_idempotent_recall():
    """Two consecutive PUT /save calls return the same response; second is no-op equivalent.

    The endpoint is idempotent by design (INSERT ... ON CONFLICT DO UPDATE SET state =
    'to_read') — calling it twice keeps state at 'to_read'.
    """
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = FakeRecord(id=1)  # paper-exists check succeeds
    conn.fetchval.return_value = "inbox"  # _assert_paper_in_states precondition (W1.7-B)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None)))

    result1 = await papers.save_paper.__wrapped__(request, 1, db_pool=pool)
    result2 = await papers.save_paper.__wrapped__(request, 1, db_pool=pool)

    assert result1 == {"status": "ok", "paper_id": 1}
    assert result2 == {"status": "ok", "paper_id": 1}
    # Both calls issued the same state = 'to_read' upsert.
    assert all(
        any("to_read" in [str(arg) for arg in call.args] for call in conn.execute.await_args_list)
        for _ in [result1, result2]
    )


@pytest.mark.asyncio
async def test_save_paper_404_when_paper_missing():
    """PUT /save/{paper_id} with no matching row raises HTTPException 404."""
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = None  # paper-exists check fails
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None)))

    with pytest.raises(HTTPException) as exc_info:
        await papers.save_paper.__wrapped__(request, 99999, db_pool=pool)

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_skip_paper_idempotent_recall():
    """Two consecutive PUT /skip calls both succeed; state stays 'done'.

    Note: W1.7-B added an inbox-only precondition; in production a real second
    skip would 409. This test mocks fetchval to "inbox" so both calls pass the
    precondition, exercising the SQL upsert idempotency at the layer it owns.
    """
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = FakeRecord(id=1)
    conn.fetchval.return_value = "inbox"  # _assert_paper_in_states precondition (W1.7-B)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None)))

    result1 = await papers.skip_paper.__wrapped__(request, 1, db_pool=pool)
    result2 = await papers.skip_paper.__wrapped__(request, 1, db_pool=pool)

    assert result1 == {"status": "ok", "paper_id": 1}
    assert result2 == {"status": "ok", "paper_id": 1}
    assert all(
        any("done" in [str(arg) for arg in call.args] for call in conn.execute.await_args_list)
        for _ in [result1, result2]
    )


@pytest.mark.asyncio
async def test_skip_paper_404():
    """PUT /skip/{paper_id} with no matching row raises HTTPException 404."""
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = None
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None)))

    with pytest.raises(HTTPException) as exc_info:
        await papers.skip_paper.__wrapped__(request, 99999, db_pool=pool)

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_reading_paper_idempotent_recall():
    """Two consecutive PUT /reading calls both succeed; state stays 'reading'."""
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = FakeRecord(id=1)
    conn.fetchval.return_value = "to_read"  # _assert_paper_in_states precondition (W1.7-B)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None)))

    result1 = await papers.reading_paper.__wrapped__(request, 1, db_pool=pool)
    result2 = await papers.reading_paper.__wrapped__(request, 1, db_pool=pool)

    assert result1 == {"status": "ok", "paper_id": 1}
    assert result2 == {"status": "ok", "paper_id": 1}
    assert any(
        "reading" in [str(arg) for arg in call.args] for call in conn.execute.await_args_list
    )


@pytest.mark.asyncio
async def test_reading_paper_404():
    """PUT /reading/{paper_id} with no matching row raises HTTPException 404."""
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = None
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None)))

    with pytest.raises(HTTPException) as exc_info:
        await papers.reading_paper.__wrapped__(request, 99999, db_pool=pool)

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_done_paper_idempotent_recall():
    """Two consecutive PUT /done calls both succeed; state stays 'done'."""
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = FakeRecord(id=1)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None)))

    result1 = await papers.done_paper.__wrapped__(request, 1, db_pool=pool)
    result2 = await papers.done_paper.__wrapped__(request, 1, db_pool=pool)

    assert result1 == {"status": "ok", "paper_id": 1}
    assert result2 == {"status": "ok", "paper_id": 1}
    assert any("done" in [str(arg) for arg in call.args] for call in conn.execute.await_args_list)


@pytest.mark.asyncio
async def test_done_paper_404():
    """PUT /done/{paper_id} with no matching row raises HTTPException 404."""
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = None
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None)))

    with pytest.raises(HTTPException) as exc_info:
        await papers.done_paper.__wrapped__(request, 99999, db_pool=pool)

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_star_paper_idempotent_recall():
    """Two consecutive PUT /star calls both succeed; starred stays TRUE; state not touched.

    Orthogonality: the upsert in star_paper does NOT write a state column.
    Group B rewrote star_paper to use a CTE + RETURNING fetchrow (not execute).
    Each call issues 2 fetchrow calls: paper-existence check + CTE RETURNING.
    """
    pool, conn = _make_pool_and_conn()
    # Each star_paper call: fetchrow[0]=paper exists, fetchrow[1]=CTE result.
    # Both calls — 4 fetchrow calls total, cycling through [paper, cte, paper, cte].
    conn.fetchrow.side_effect = [
        FakeRecord(id=1),
        FakeRecord(is_new_row=True, prev_starred=False),
        FakeRecord(id=1),
        FakeRecord(is_new_row=False, prev_starred=True),
    ]
    conn.fetchval.return_value = 0  # project_papers COUNT — no zotero.push needed
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None)))

    with patch.object(papers, "assert_paper_ownership", AsyncMock(return_value=None)):
        result1 = await papers.star_paper.__wrapped__(request, 1, db_pool=pool)
        result2 = await papers.star_paper.__wrapped__(request, 1, db_pool=pool)

    assert result1 == {"status": "ok", "paper_id": 1}
    assert result2 == {"status": "ok", "paper_id": 1}
    # Group B: upsert is now via conn.fetchrow (CTE WITH RETURNING), not conn.execute.
    fetchrow_sql_calls = [call.args[0] for call in conn.fetchrow.await_args_list]
    upsert_sql = next(
        (sql for sql in fetchrow_sql_calls if "INSERT INTO paper_user_state" in sql), None
    )
    assert upsert_sql is not None, (
        f"Expected CTE upsert in fetchrow calls; got {fetchrow_sql_calls}"
    )
    do_update_clause = upsert_sql.split("DO UPDATE SET", 1)[-1]
    assert "state =" not in do_update_clause


@pytest.mark.asyncio
async def test_star_paper_404():
    """PUT /star/{paper_id} with no matching row raises HTTPException 404."""
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = None
    conn.fetchval.return_value = 0
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None)))

    with pytest.raises(HTTPException) as exc_info:
        await papers.star_paper.__wrapped__(request, 99999, db_pool=pool)

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_unstar_paper_idempotent_recall():
    """Two consecutive PUT /unstar calls both succeed; starred stays FALSE."""
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = FakeRecord(id=1)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None)))

    result1 = await papers.unstar_paper.__wrapped__(request, 1, db_pool=pool)
    result2 = await papers.unstar_paper.__wrapped__(request, 1, db_pool=pool)

    assert result1 == {"status": "ok", "paper_id": 1}
    assert result2 == {"status": "ok", "paper_id": 1}
    # Both calls should write starred = FALSE.
    sql_calls = [call.args[0] for call in conn.execute.await_args_list]
    assert any("INSERT INTO paper_user_state" in sql and "starred" in sql for sql in sql_calls)
    assert any(False in call.args for call in conn.execute.await_args_list)


@pytest.mark.asyncio
async def test_unstar_paper_404():
    """PUT /unstar/{paper_id} with no matching row raises HTTPException 404."""
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = None
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None)))

    with pytest.raises(HTTPException) as exc_info:
        await papers.unstar_paper.__wrapped__(request, 99999, db_pool=pool)

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_annotate_paper_404_when_paper_missing():
    """PUT /annotations/{paper_id} raises 404 when paper FK constraint fires.

    Ownership is covered elsewhere; here it is a pass-through so the INSERT
    into paper_user_state raises ForeignKeyViolationError which is mapped
    to 404.
    """
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.side_effect = asyncpg.ForeignKeyViolationError("missing paper")
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None)))

    with (
        patch.object(papers, "assert_paper_ownership", AsyncMock(return_value=None)),
        pytest.raises(HTTPException) as exc_info,
    ):
        await papers.annotate_paper.__wrapped__(
            request,
            99999,
            body=AnnotationsRequest(rating=3),
            db_pool=pool,
        )

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_annotate_paper_unauthorized_user():
    """PUT /annotations with a caller who does not own the paper returns 403.

    Mirrors the pattern in test_get_paper_detail_403_for_other_user: the paper
    is owned by user 42, the caller is user 99 → assert_paper_ownership raises 403.
    """
    pool, conn = _make_pool_and_conn()
    # Sprint B: assert_paper_ownership reads discovered_by (with legacy
    # user_id fallback for fixtures), then checks user_library membership.
    conn.fetchrow.return_value = FakeRecord(user_id=42)
    conn.fetchval = AsyncMock(return_value=None)  # not in caller's library
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None)))

    with pytest.raises(HTTPException) as exc_info:
        await papers.annotate_paper.__wrapped__(
            request,
            1,
            body=AnnotationsRequest(rating=3),
            db_pool=pool,
            user_id=99,
        )

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_bulk_action_idempotent_recall():
    """Two consecutive bulk {action: 'save', paper_ids: [1, 2, 3]} succeed;
    final state is 'to_read' for all papers (idempotent ON CONFLICT upsert).
    """
    pool, conn = _make_pool_and_conn()
    body = BulkActionRequest(paper_ids=[1, 2, 3], action="save")
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None)))

    result1 = await papers.bulk_action_papers.__wrapped__(request, body, db_pool=pool)
    result2 = await papers.bulk_action_papers.__wrapped__(request, body, db_pool=pool)

    assert result1 == {"succeeded": [1, 2, 3], "failed": []}
    assert result2 == {"succeeded": [1, 2, 3], "failed": []}
    assert any(
        "to_read" in [str(arg) for arg in call.args] for call in conn.execute.await_args_list
    )


@pytest.mark.asyncio
async def test_bulk_action_partial_idempotent_with_invalid_id():
    """Bulk {action: 'save', paper_ids: [1, 99999]} with paper 99999 missing.

    Per-paper savepoints isolate the failure so paper 1 succeeds while
    99999 is recorded in 'failed'.  The overall response is HTTP 200 with
    the succeeded/failed partition — NOT a 207 (the bulk endpoint always
    returns 200 in this implementation).
    """
    pool, conn = _make_pool_and_conn()

    async def _fetchrow(sql: str, *args, **kwargs):
        del kwargs
        # Sprint B: ownership probe selects ``discovered_by`` (mocks may still
        # use the legacy ``user_id`` key — the helper falls back to it).
        if "FROM papers WHERE id" in sql or "SELECT discovered_by FROM papers" in sql:
            paper_id = args[0]
            if paper_id == 99999:
                return None  # paper not found → assert_paper_ownership raises 404
            return FakeRecord(user_id=99)  # caller is also discoverer
        return None

    conn.fetchrow.side_effect = _fetchrow

    body = BulkActionRequest(paper_ids=[1, 99999], action="save")
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None)))

    result = await papers.bulk_action_papers.__wrapped__(request, body, db_pool=pool, user_id=99)

    assert result["succeeded"] == [1]
    assert len(result["failed"]) == 1
    assert result["failed"][0]["paper_id"] == 99999
    assert "error" in result["failed"][0]


# ---------------------------------------------------------------------------
# recommendation_feedback endpoints — 404 / empty-list edge cases (Task A.1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_recommendation_feedback_404_for_nonexistent_paper_filter():
    """GET /api/recommendation_feedback?paper_id=99999 returns 200 with items=[] and total=0.

    The endpoint does NOT raise 404 for a paper_id that has no feedback rows;
    it returns an empty paginated list.
    """
    from paper_ingestion.routers import recommendation_feedback as rf_router

    pool, conn = _make_pool_and_conn()
    conn.fetch.return_value = []
    conn.fetchval.return_value = 0

    result = await rf_router.list_recommendation_feedback.__wrapped__(
        request=MagicMock(),
        paper_id=99999,
        limit=50,
        offset=0,
        db_pool=pool,
        user_id=None,
    )

    assert result.items == []
    assert result.total == 0


@pytest.mark.asyncio
async def test_delete_recommendation_feedback_returns_zero_for_nonexistent_topic():
    """DELETE /api/recommendation_feedback?topic_id=99999 returns {deleted: 0}.

    When no rows match the topic_id + user_id predicate, asyncpg returns
    'DELETE 0' and the response is DeleteFeedbackResponse(deleted=0, topic_id=99999).
    """
    from paper_ingestion.routers import recommendation_feedback as rf_router

    pool, conn = _make_pool_and_conn()
    conn.execute.return_value = "DELETE 0"

    result = await rf_router.delete_recommendation_feedback_by_topic.__wrapped__(
        request=MagicMock(),
        topic_id=99999,
        db_pool=pool,
        user_id=None,
    )

    assert result.deleted == 0
    assert result.topic_id == 99999


# ---------------------------------------------------------------------------
# DELETE /api/papers/{paper_id}/feedback  (UX-E.1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_paper_feedback_returns_204_for_existing_row():
    """DELETE /api/papers/{id}/feedback?source=pulse_thumbs deletes the matching row.

    Returns 204 No Content regardless of row count.  We verify the DELETE SQL
    is called with the correct paper_id, user_id, and source.
    """
    pool, conn = _make_pool_and_conn()
    conn.execute.return_value = "DELETE 1"

    result = await papers.delete_paper_feedback.__wrapped__(
        request=MagicMock(),
        paper_id=42,
        source="pulse_thumbs",
        db_pool=pool,
        user_id=7,
    )

    # 204 handler returns None
    assert result is None
    # Verify the DELETE SQL was called with the right args
    conn.execute.assert_awaited_once()
    call_args = conn.execute.await_args
    sql = call_args.args[0]
    assert "DELETE FROM recommendation_feedback" in sql
    assert "paper_id = $1" in sql
    assert "IS NOT DISTINCT FROM" not in sql
    assert "user_id = $2" in sql
    assert "source = $3" in sql
    positional = list(call_args.args[1:])
    assert positional[0] == 42
    assert positional[1] == 7  # exact user scope (no NULL-shared rows)
    assert positional[2] == "pulse_thumbs"


@pytest.mark.asyncio
async def test_delete_paper_feedback_returns_204_for_nonexistent_row():
    """DELETE /api/papers/{id}/feedback is idempotent — returns 204 even when no row exists."""
    pool, conn = _make_pool_and_conn()
    conn.execute.return_value = "DELETE 0"  # no row matched

    result = await papers.delete_paper_feedback.__wrapped__(
        request=MagicMock(),
        paper_id=99,
        source="dismiss_combined",
        db_pool=pool,
        user_id=None,
    )

    assert result is None
    conn.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_delete_paper_feedback_scoped_to_exact_user():
    """WS-CROSS-USER: DELETE scopes recommendation_feedback by exact user_id.

    The pre-WS-CROSS-USER behaviour matched NULL-owner rows via
    ``IS NOT DISTINCT FROM`` — a cross-user deletion vector for API-key-only
    callers. The resolver now always yields a real user and the DELETE binds
    that user with an exact ``user_id = $2`` predicate.
    """
    pool, conn = _make_pool_and_conn()
    conn.execute.return_value = "DELETE 1"

    await papers.delete_paper_feedback.__wrapped__(
        request=MagicMock(),
        paper_id=7,
        source="feed_thumbs",
        db_pool=pool,
        user_id=13,
    )

    call_args = conn.execute.await_args
    sql = call_args.args[0]
    assert "IS NOT DISTINCT FROM" not in sql
    assert "user_id = $2" in sql
    positional = list(call_args.args[1:])
    assert positional[1] == 13


@pytest.mark.asyncio
async def test_delete_paper_feedback_different_user_id_not_deleted():
    """DELETE is scoped to the caller's user_id — a different user's row is untouched.

    This test verifies that the SQL parameters carry the caller's user_id.
    A row owned by user 42 is NOT deleted when the caller is user 99.
    (The DB enforces scoping; we verify we pass the correct user_id arg.)
    """
    pool, conn = _make_pool_and_conn()
    conn.execute.return_value = "DELETE 0"  # user 99 has no row for paper 42

    await papers.delete_paper_feedback.__wrapped__(
        request=MagicMock(),
        paper_id=42,
        source="pulse_thumbs",
        db_pool=pool,
        user_id=99,
    )

    call_args = conn.execute.await_args
    positional = list(call_args.args[1:])
    # The SQL must be parameterised with user_id=99, not 42
    assert positional[1] == 99


# ---------------------------------------------------------------------------
# PUT /api/papers/{paper_id}/unsave  (UX-E.2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unsave_paper_from_to_read_returns_200():
    """PUT /api/papers/{id}/unsave transitions to_read → inbox and returns 200.

    conn.fetchval returns 'to_read' so _assert_paper_in_state passes; the
    subsequent _upsert_state_and_starred must write state='inbox'.
    """
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = FakeRecord(user_id=None)  # ownership check
    conn.fetchval.return_value = "to_read"  # _assert_paper_in_state
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None)))

    result = await papers.unsave_paper.__wrapped__(request, 42, db_pool=pool)

    assert result == {"status": "ok", "paper_id": 42}
    sql_calls = [call.args[0] for call in conn.execute.await_args_list]
    assert any("INSERT INTO paper_user_state" in sql and "state" in sql for sql in sql_calls), (
        f"Expected an INSERT writing state; got: {sql_calls}"
    )
    assert any(
        "inbox" in [str(arg) for arg in call.args] for call in conn.execute.await_args_list
    ), f"Expected 'inbox' in execute args; got: {conn.execute.await_args_list!r}"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_state", ["inbox", "reading", "done", "trash"])
async def test_unsave_paper_from_wrong_state_returns_409(bad_state: str):
    """PUT /api/papers/{id}/unsave raises 409 when paper is not in to_read.

    Each non-to_read state must yield HTTP 409 with a detail string that
    contains both 'to_read' (the required state) and the current state.
    """
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = FakeRecord(user_id=None)  # ownership check
    conn.fetchval.return_value = bad_state  # _assert_paper_in_state
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None)))

    with pytest.raises(HTTPException) as exc_info:
        await papers.unsave_paper.__wrapped__(request, 42, db_pool=pool)

    assert exc_info.value.status_code == 409
    assert "to_read" in exc_info.value.detail
    assert bad_state in exc_info.value.detail


# ---------------------------------------------------------------------------
# POST /api/papers/process_batch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_batch_happy_path_returns_job_id():
    """Happy path: valid paper_ids list enqueues job and returns job UUID."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    import jarvis_common.task_registry as task_registry

    pool = _make_pool_and_conn()[0]
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None)))
    mock_task = MagicMock()
    mock_defer = AsyncMock()
    mock_task.defer_async = mock_defer
    with patch.dict(task_registry.KIND_TO_TASK, {"papers.batch_process": mock_task}):
        from paper_ingestion.models import ProcessBatchRequest

        body = ProcessBatchRequest(paper_ids=[1, 2, 3])
        result = await papers.process_batch.__wrapped__(request, body, db_pool=pool, user_id=None)

    assert result["status"] == "queued"
    assert "job_id" in result
    mock_defer.assert_awaited_once()
    assert mock_defer.await_args is not None
    call_kwargs = mock_defer.await_args.kwargs
    assert call_kwargs.get("paper_ids") == [1, 2, 3]
    assert "job_id" in call_kwargs


@pytest.mark.asyncio
async def test_process_batch_empty_list_raises_422():
    """An empty paper_ids list should fail Pydantic validation (min_length=1)."""
    import pydantic
    from paper_ingestion.models import ProcessBatchRequest

    with pytest.raises(pydantic.ValidationError):
        ProcessBatchRequest(paper_ids=[])


@pytest.mark.asyncio
async def test_process_batch_over_50_raises_422():
    """More than 50 paper IDs should fail Pydantic validation (max_length=50)."""
    import pydantic
    from paper_ingestion.models import ProcessBatchRequest

    with pytest.raises(pydantic.ValidationError):
        ProcessBatchRequest(paper_ids=list(range(51)))
