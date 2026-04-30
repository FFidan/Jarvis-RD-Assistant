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
    paper_conv.assert_called_once()
    summary_conv.assert_called_once()
    chunk_conv.assert_called_once()
    conn.fetchval.assert_awaited_once()


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

    with patch.object(papers, "row_to_paper_response", return_value=paper_model):
        result = await papers.get_paper_detail.__wrapped__(
            MagicMock(),
            paper_id=42,
            db_pool=pool,
        )

    assert result.user_state is not None
    assert result.user_state.state == "inbox"
    assert result.user_state.starred is True


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

    with patch.object(papers, "row_to_paper_response", return_value=paper_model):
        result = await papers.get_paper_detail.__wrapped__(
            MagicMock(),
            paper_id=4,
            db_pool=pool,
        )

    assert result.has_project_links is False
    conn.fetchval.assert_awaited_once()


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
    INSERT into a stable 404."""
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.side_effect = asyncpg.ForeignKeyViolationError("missing paper")
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
    """submit_feedback writes a recommendation_feedback row with ON CONFLICT
    (paper_id, user_id, source) DO UPDATE — the spec'd upsert."""
    pool, conn = _make_pool_and_conn()
    now = datetime.now(UTC)
    conn.fetchrow.return_value = FakeRecord(
        paper_id=7,
        signal="negative",
        source="feed_thumbs",
        created_at=now,
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None)))

    result = await papers.submit_feedback.__wrapped__(
        request,
        paper_id=7,
        body=FeedbackRequest(signal="negative", source="feed_thumbs", reason="off-topic"),
        db_pool=pool,
    )

    sql_calls = [call.args[0] for call in conn.fetchrow.await_args_list]
    assert any(
        "INSERT INTO recommendation_feedback" in sql
        and "ON CONFLICT (paper_id, user_id, source) DO UPDATE" in sql
        for sql in sql_calls
    ), f"Expected recommendation_feedback upsert; got SQL calls: {sql_calls}"
    assert result.signal == "negative"
    assert result.source == "feed_thumbs"


# ---------------------------------------------------------------------------
# list_papers — positional parameter wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_papers_no_filters_uses_limit_offset():
    """list_papers with no filters should still pass LIMIT/OFFSET as positional params."""
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
        view=None,
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
async def test_list_papers_view_and_source_type_correct_param_indices():
    """list_papers with view + source_type binds user_id at $1 (for the LEFT
    JOIN's user-scoping) and source_type at $2; LIMIT $3, OFFSET $4.

    Note: ``view`` itself is a literal substitution from VIEW_PREDICATES
    and does NOT consume a positional param — the user_id binding does.
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
    )

    fetch_call = conn.fetch.await_args
    sql, *positional = fetch_call.args
    # $1 is user_id (None in single-user mode), $2 is source_type, then $3/$4 for LIMIT/OFFSET.
    assert "$1::int IS NULL OR pus.user_id IS NOT DISTINCT FROM $1" in sql
    assert "p.source_type = $2" in sql
    assert "LIMIT $3" in sql
    assert "OFFSET $4" in sql
    assert "COALESCE(pus.state, 'inbox') = 'reading'" in sql
    assert positional == [None, "arxiv", 10, 5]


@pytest.mark.asyncio
async def test_list_papers_all_filters_correct_param_indices():
    """list_papers with topic_id + view + source_type + q binds in order:

    $1=topic_id, $2=user_id (for LEFT JOIN), $3=source_type.value,
    $4=q, $5=LIMIT, $6=OFFSET. ``view`` is a literal predicate, no
    positional binding.
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
    )

    fetch_call = conn.fetch.await_args
    sql, *positional = fetch_call.args
    assert "pt.topic_id = $1" in sql
    assert "$2::int IS NULL OR pus.user_id IS NOT DISTINCT FROM $2" in sql
    assert "p.source_type = $3" in sql
    assert "plainto_tsquery" in sql and "$4" in sql
    assert "LIMIT $5" in sql
    assert "OFFSET $6" in sql
    assert positional == [7, None, "arxiv", "neural", 15, 3]


# ---------------------------------------------------------------------------
# WS-6B-α — multi-user ownership wiring on paper-ID endpoints.
# Single-user mode is exercised by every other test (user_id=None bypass).
# ---------------------------------------------------------------------------


async def _async_user_99(_request):
    del _request
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


# ---------------------------------------------------------------------------
# Single-paper lifecycle endpoints — Phase A Wave 1ab (state mutators)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_paper_sets_state_to_read():
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = FakeRecord(id=42)  # paper-exists check
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
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None)))

    result = await papers.skip_paper.__wrapped__(request, 42, db_pool=pool)

    assert result == {"status": "ok", "paper_id": 42}
    assert any("done" in [str(arg) for arg in call.args] for call in conn.execute.await_args_list)


@pytest.mark.asyncio
async def test_reading_paper_sets_state_reading():
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = FakeRecord(id=42)
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
    """``star`` writes ``starred = $N`` only — no ``state =`` clause."""
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = FakeRecord(id=42)
    # B6 added a fetchval for project_papers COUNT after the upsert.
    # Return 0 so no zotero.push is enqueued and the comparison succeeds.
    conn.fetchval.return_value = 0
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None)))

    result = await papers.star_paper.__wrapped__(request, 42, db_pool=pool)

    assert result == {"status": "ok", "paper_id": 42}
    sql_calls = [call.args[0] for call in conn.execute.await_args_list]
    upsert_sql = next((sql for sql in sql_calls if "INSERT INTO paper_user_state" in sql), None)
    assert upsert_sql is not None, f"Expected an upsert SQL; got {sql_calls}"
    assert "starred" in upsert_sql
    # The DO UPDATE SET clause must NOT touch state.
    do_update_clause = upsert_sql.split("DO UPDATE SET", 1)[-1]
    assert "state =" not in do_update_clause, (
        f"star_paper must not write state; DO UPDATE SET = {do_update_clause!r}"
    )
    # True flag must be in execute args.
    assert any(True in call.args for call in conn.execute.await_args_list)


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
    """The atomic UPDATE branch sets state_before_trash := paper_user_state.state
    while writing state := 'trash' in a single statement — no read-then-write race."""
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
    assert "state_before_trash = paper_user_state.state" in trash_sql
    assert "state = 'trash'" in trash_sql


@pytest.mark.asyncio
async def test_restore_paper_returns_state_before_trash_to_state():
    """Restore: ``state := COALESCE(state_before_trash, 'inbox')`` and
    ``state_before_trash := NULL`` — the COALESCE handles the
    null-state_before_trash case implicitly (defensive default)."""
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = FakeRecord(id=42)
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
    body = BulkActionRequest(paper_ids=[1], action="restore")
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None)))

    result = await papers.bulk_action_papers.__wrapped__(request, body, db_pool=pool)

    assert result["succeeded"] == [1]
    sql_calls = [call.args[0] for call in conn.execute.await_args_list]
    assert any("COALESCE(state_before_trash, 'inbox')" in sql for sql in sql_calls), (
        f"Expected restore SQL pattern; got {sql_calls}"
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
    assert any(
        "state_before_trash = paper_user_state.state" in sql and "state = 'trash'" in sql
        for sql in sql_calls
    ), f"Expected atomic trash SQL; got {sql_calls}"


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
async def test_bulk_partial_failure_records_savepoint_isolation(monkeypatch):
    """Per-paper savepoints must isolate failures: paper 2 raises, papers
    1 and 3 still succeed (the outer txn commits, only paper 2's
    savepoint rolls back)."""
    monkeypatch.setattr("paper_ingestion.routers.papers.current_user_id_or_none", _async_user_99)
    pool, conn = _make_pool_and_conn()

    # Build a side_effect for assert_paper_ownership that fails on paper_id=2.
    # The router calls conn.fetchrow("SELECT user_id FROM papers WHERE id = $1", paper_id)
    # — return user_id=99 for {1,3} and a mismatched 999 for paper 2 to trigger 403.
    fetched: list[FakeRecord] = []

    async def _fetchrow(sql: str, *args, **kwargs):
        del kwargs  # unused but required by asyncpg.Connection.fetchrow signature
        if "SELECT user_id FROM papers" in sql:
            paper_id = args[0]
            if paper_id == 2:
                fetched.append(FakeRecord(user_id=999))
                return FakeRecord(user_id=999)  # mismatch → 403 from helper
            fetched.append(FakeRecord(user_id=99))
            return FakeRecord(user_id=99)
        return None

    conn.fetchrow.side_effect = _fetchrow

    body = BulkActionRequest(paper_ids=[1, 2, 3], action="save")
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None)))

    result = await papers.bulk_action_papers.__wrapped__(request, body, db_pool=pool)

    assert result["succeeded"] == [1, 3]
    assert len(result["failed"]) == 1
    assert result["failed"][0]["paper_id"] == 2
    assert "error" in result["failed"][0]
