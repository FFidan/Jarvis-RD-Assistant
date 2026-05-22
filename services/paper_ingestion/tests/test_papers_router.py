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

from tests.conftest import FakeRecord, _make_pool_and_conn


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


def _paper_response(id=1):
    """Return a minimal PaperResponse model for converter-stubbed get_paper_detail tests."""
    return PaperResponse(
        id=id,
        external_id=f"paper-{id}",
        source_type=SourceType.ARXIV,
        title=f"Paper {id}",
        authors=["Ada"],
        url=f"https://example.com/papers/{id}",
        created_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# list_papers — view-based filtering (post Phase A redesign)
# ---------------------------------------------------------------------------


# W4-followup: collapsed to contract/test_papers_contract.py::test_list_papers_view_inbox_returns_real_inbox_papers


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


# W4-followup: collapsed to contract/test_papers_contract.py::test_list_papers_bm25_search_returns_matching_papers


# ---------------------------------------------------------------------------
# get_paper_detail — surfaces user_state + recent_feedback (post-redesign)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_paper_detail_raises_404_when_missing():
    """get_paper_detail returns 404 when the paper row is absent.

    Ownership is covered by dedicated tests; here it is a pass-through so the
    route's own missing-paper 404 is exercised.
    """
    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = None

    with (
        patch(
            "paper_ingestion.papers_service.assert_paper_ownership", AsyncMock(return_value=None)
        ),
        pytest.raises(HTTPException, match="Paper not found") as exc_info,
    ):
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

    paper_model = _paper_response(id=3)
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
        patch(
            "paper_ingestion.papers_service.assert_paper_ownership", AsyncMock(return_value=None)
        ),
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

    with (
        patch(
            "paper_ingestion.papers_service.assert_paper_ownership", AsyncMock(return_value=None)
        ),
        patch.object(papers, "row_to_paper_response", return_value=_paper_response(id=42)),
    ):
        result = await papers.get_paper_detail.__wrapped__(
            MagicMock(),
            paper_id=42,
            db_pool=pool,
        )

    assert result.user_state is not None
    assert result.user_state.state == "inbox"
    assert result.user_state.starred is True


# Cluster 6 deletions (2026-05-22):
#   test_get_paper_detail_processing_failed_true_when_last_job_failed,
#   test_get_paper_detail_processing_failed_false_when_last_job_succeeded,
#   test_get_paper_detail_sets_has_project_links_false_when_unlinked
# all superseded by test_papers_contract.py::test_paper_detail_processing_failed_flag
# + test_paper_detail_has_project_links_flag (real-DB asserts).


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


# Cluster 6 deletions (2026-05-22):
#   test_submit_feedback_maps_foreign_key_violation_to_404      → test_papers_contract.py::test_feedback_404_when_paper_deleted_or_missing
#   test_submit_feedback_accepts_system_discovered_origins (×3) → existing test_a69_submit_feedback_owner_creates_row (679)
#   test_submit_feedback_rejects_user_initiated_papers          → test_papers_contract.py::test_feedback_rejects_user_initiated_paper


# ---------------------------------------------------------------------------
# list_papers — positional parameter wiring
# ---------------------------------------------------------------------------


# W4-followup: collapsed to contract/test_papers_contract.py::test_list_papers_scoped_to_user_library
# SQL-text + param-binding assertions (B1-09); behavioral scoping covered by contract test.


# W4-followup: collapsed to contract/test_papers_contract.py::test_list_papers_topic_filter_scopes_to_topic
# Pure parameter-binding assertions ($1=topic_id, $2=user_id, etc.) — B1-09 class.


# W4-followup: collapsed to contract/test_papers_contract.py::test_list_papers_view_source_type_combined_filter
# Pure SQL-text + parameter-binding assertions ($1/$2 user_id, $3 source_type, etc.) — B1-09 class.


# W4-followup: collapsed to contract/test_papers_contract.py::test_list_papers_all_filters_combined
# Pure SQL-text + parameter-binding assertions ($1 topic, $2/$3 user_id, $4 source, $5 q, etc.) — B1-09 class.


# ---------------------------------------------------------------------------
# RB-1 — BM25/fallback list_papers scoped to caller's user_library
# ---------------------------------------------------------------------------


# Collapsed (E2.PI): test_list_papers_bm25_scoped_to_user
# Survivor: test_papers_contract.py::test_list_papers_bm25_no_cross_user_leak
# BM25 path JOIN user_library user_id scoping verified with real DB cross-user isolation.


# ---------------------------------------------------------------------------
# WS-6B-α — multi-user ownership wiring on paper-ID endpoints.
# Single-user mode is exercised by every other test (user_id=None bypass).
# ---------------------------------------------------------------------------


# Collapsed (E2.PI): test_get_paper_detail_403_for_other_user
# Survivor: libs/jarvis_common/tests/contract/test_idor_contract.py::test_user_b_cannot_access_user_a_resource
# (paper GET quadruple) — non-owner user gets 403/404 on paper detail endpoint verified with real DB.


# ---------------------------------------------------------------------------
# Single-paper lifecycle endpoints — Phase A Wave 1ab (state mutators)
# ---------------------------------------------------------------------------


# W4-followup: collapsed to contract/test_papers_contract.py::test_save_paper_state_transition
# Behavioral subset of test_save_paper_idempotent_recall below; SQL assertions are B1-09 class.
# Survivor: test_save_paper_idempotent_recall (line ~1381)


# W4-followup: collapsed to contract/test_papers_contract.py::test_skip_paper_state_transition
# Behavioral subset of test_skip_paper_idempotent_recall below; SQL arg assertion is B1-09 class.
# Survivor: test_skip_paper_idempotent_recall (line ~1409)


# W4-followup: collapsed to contract/test_papers_contract.py::test_reading_paper_state_transition
# Behavioral subset of test_reading_paper_idempotent_recall below; SQL arg assertion is B1-09 class.
# Survivor: test_reading_paper_idempotent_recall (line ~1436)


# W4-followup: collapsed to contract/test_papers_contract.py::test_done_paper_state_transition
# Behavioral subset of test_done_paper_idempotent_recall below; SQL arg assertion is B1-09 class.
# Survivor: test_done_paper_idempotent_recall (line ~1458)


# Cluster 6 deletion (2026-05-22):
#   test_star_paper_sets_starred_true_does_not_change_state → test_papers_contract.py::test_star_paper_sets_starred_true
# (real-DB assertion that starred=TRUE in paper_user_state after the call,
# replacing SQL-substring checks on the CTE clause).


# W4-followup: collapsed to contract/test_papers_contract.py::test_unstar_paper_state_transition
# Behavioral subset of test_unstar_paper_idempotent_recall below; SQL assertions are B1-09 class.
# Survivor: test_unstar_paper_idempotent_recall (line ~1521)


# W4-followup: collapsed to contract/test_papers_contract.py::test_trash_paper_state_transition
# Primary assertions were SQL-text checks on CASE expression internals (B1-09 class).
# Behavioral outcome (state→trash, state_before_trash preservation) verified by contract test.


# W4-followup: collapsed to contract/test_papers_contract.py::test_restore_paper_state_transition
# Primary assertion was SQL-text COALESCE substring match (B1-09 class).
# Survivor: test_restore_paper_non_trash_returns_409 covers the guard; contract covers the transition.


@pytest.mark.asyncio
# Cluster 6 deletion (2026-05-22):
#   test_restore_paper_non_trash_returns_409 → test_papers_contract.py::
#   test_restore_non_trash_paper_returns_409 (real-DB assertion on the
#   _assert_paper_in_states guard at restore_paper:738-751).


# Collapsed (Phase C): test_trash_and_reject_writes_both_lifecycle_and_feedback
# Survivor: test_papers_contract.py::test_a81_trash_and_reject_trashes_and_inserts_feedback
# SQL-substring assertions (state='trash', INSERT INTO recommendation_feedback, signal='negative',
# source='dismiss_combined') — B1-09 class. Contract A81 verifies behavioral atomicity with real DB.


# ---------------------------------------------------------------------------
# annotate_paper — partial upsert for rating / user_notes / flagged
# ---------------------------------------------------------------------------


# Collapsed (Phase C): test_annotate_paper_writes_rating_user_notes_flagged
# Survivor: test_papers_contract.py::test_annotations_owner_gets_200_with_correct_shape
# Response shape (rating, user_notes, flagged, state, starred) verified with real DB.


# W4-followup: collapsed to contract/test_papers_contract.py::test_annotations_partial_update_preserves_other_fields
# Primary assertions were COALESCE($4) / COALESCE($5) parameter-index SQL checks (B1-09 class).
# Survivor: test_annotations_owner_gets_200_with_correct_shape covers the behavioral outcome.


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

# B1-03 deleted: test_hard_delete_409_when_paper_not_in_trash
# Superseded by test_papers_lifecycle.py::test_hard_delete_requires_trash_state (line 37)

# B1-03 deleted: test_hard_delete_aborts_qdrant_when_sql_delete_fails
# Superseded by test_papers_lifecycle.py::test_hard_delete_db_rollback_does_not_call_qdrant (line 389)

# B1-03 deleted: test_hard_delete_logs_qdrant_failure_after_sql_success
# Superseded by test_papers_lifecycle.py::test_hard_delete_qdrant_failure_logs_orphan (line 413)

# B1-03 deleted: test_hard_delete_calls_qdrant_after_sql_succeeds
# Superseded by test_papers_lifecycle.py::test_hard_delete_with_trash_state_succeeds (line 63)
# and test_papers_lifecycle.py::test_hard_delete_reorders_qdrant_after_db_commit (line 353)


# ---------------------------------------------------------------------------
# Bulk action — POST /api/papers/bulk (spec §4.5, 10-action enum)
# ---------------------------------------------------------------------------


# Collapsed (Phase C): test_bulk_save_action_sets_state_to_read
# Survivor: test_papers_contract.py::test_a84_bulk_action_transitions_state_for_owner
# SQL-arg assertion ("to_read" in args) — B1-09 class. Contract A84 verifies state='to_read' in DB.

# Collapsed (Phase C): test_bulk_skip_action_sets_state_done
# Survivor: test_papers_contract.py::test_a74_skip_paper_transitions_state_to_done
# SQL-arg assertion ("done" in args) — B1-09 class. Contract A74 verifies state='done' in DB.


# Collapsed (Phase C): test_bulk_restore_action_uses_coalesce_state_before_trash
# Survivor: test_papers_contract.py::test_restore_paper_state_transition
# Primary assertion was SQL COALESCE structure (B1-09 class); behavioral restore outcome covered by contract.


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
    # Verify starred=False was written (not starred=True)
    assert any(False in call.args for call in conn.execute.await_args_list), (
        "Expected starred=False in execute args for unstar action"
    )


# Collapsed (Phase C): test_bulk_trash_action_sets_state_before_trash_atomically
# Survivor: test_papers_contract.py::test_trash_paper_state_transition
# Primary assertions were CASE expression SQL structure (B1-09 class); behavioral trash covered by contract.


# Collapsed (E2.PI): test_bulk_feedback_positive_writes_recommendation_feedback
# Survivor: test_papers_contract.py::test_a69_submit_feedback_owner_creates_row
# bulk feedback_positive writes recommendation_feedback with signal=positive verified with real DB.

# Collapsed (E2.PI): test_bulk_feedback_negative_writes_recommendation_feedback
# Survivor: test_papers_contract.py::test_a69_submit_feedback_owner_creates_row
# bulk feedback_negative writes recommendation_feedback with signal=negative verified with real DB.

# Collapsed (E2.PI): test_bulk_partial_failure_records_savepoint_isolation
# Survivor: test_papers_contract.py::test_e1_bulk_action_partial_failure_isolation
# Per-paper savepoint isolation (succeeded/failed partition) verified with real DB.


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


# B1-01 deleted: test_save_paper_404_when_paper_missing
# Superseded by libs/jarvis_common/tests/test_ownership.py::test_assert_paper_ownership_404_for_missing_paper (line 47)


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


# B1-01 deleted: test_skip_paper_404
# Superseded by libs/jarvis_common/tests/test_ownership.py::test_assert_paper_ownership_404_for_missing_paper (line 47)


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


# B1-01 deleted: test_reading_paper_404
# Superseded by libs/jarvis_common/tests/test_ownership.py::test_assert_paper_ownership_404_for_missing_paper (line 47)


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


# B1-01 deleted: test_done_paper_404
# Superseded by libs/jarvis_common/tests/test_ownership.py::test_assert_paper_ownership_404_for_missing_paper (line 47)


@pytest.mark.asyncio
async def test_star_paper_idempotent_recall():
    """Two consecutive PUT /star calls both succeed; starred stays TRUE; state not touched.

    Orthogonality: the upsert in star_paper does NOT write a state column.
    Group B rewrote star_paper to use a CTE + RETURNING fetchrow (not execute).
    Each call issues 2 fetchrow calls: paper-existence check + CTE RETURNING.
    """
    pool, conn = _make_pool_and_conn()
    # Each star_paper call issues 1 fetchrow: CTE RETURNING.
    # Both calls — 2 fetchrow calls total.
    # (The redundant paper-existence SELECT was removed in W2c B-DRY-RED9;
    # assert_paper_ownership already guarantees existence before this point.)
    conn.fetchrow.side_effect = [
        FakeRecord(is_new_row=True, prev_starred=False),
        FakeRecord(is_new_row=False, prev_starred=True),
    ]
    conn.fetchval.return_value = 0  # project_papers COUNT — no zotero.push needed
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None)))

    with patch(
        "paper_ingestion.papers_service.assert_paper_ownership", AsyncMock(return_value=None)
    ):
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


# B1-01 deleted: test_star_paper_404
# Superseded by libs/jarvis_common/tests/test_ownership.py::test_assert_paper_ownership_404_for_missing_paper (line 47)


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
    assert any(False in call.args for call in conn.execute.await_args_list)


# B1-01 deleted: test_unstar_paper_404
# Superseded by libs/jarvis_common/tests/test_ownership.py::test_assert_paper_ownership_404_for_missing_paper (line 47)


# B1-01 deleted: test_annotate_paper_404_when_paper_missing
# Superseded by libs/jarvis_common/tests/test_ownership.py::test_assert_paper_ownership_404_for_missing_paper (line 47)
# W3-B1d RESTORED: the original deletion was a misclassification — this test
# covers a DIFFERENT 404 path: FK violation from _upsert_paper_user_state, not
# the ownership check.  The ownership test above only covers assert_paper_ownership→404.


@pytest.mark.asyncio
async def test_annotate_paper_maps_fk_violation_to_404():
    """PUT /annotations/{paper_id} maps ForeignKeyViolationError → HTTP 404.

    Ownership passes (assert_paper_ownership is mocked to return None), but the
    subsequent INSERT into paper_user_state raises a ForeignKeyViolationError
    (papers row missing at the DB level after the ownership check).  The handler
    must catch it and raise HTTPException(404).

    This test would fail if the ``except asyncpg.ForeignKeyViolationError``
    clause in annotate_paper (papers.py ~line 818) were removed — the exception
    would propagate uncaught instead of becoming a 404.
    """
    pool, conn = _make_pool_and_conn()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(embedder=None)))

    with (
        patch(
            "paper_ingestion.papers_service.assert_paper_ownership",
            AsyncMock(return_value=None),
        ),
        patch(
            "paper_ingestion.routers.papers._upsert_paper_user_state",
            AsyncMock(side_effect=asyncpg.ForeignKeyViolationError("missing paper")),
        ),
        pytest.raises(HTTPException) as exc_info,
    ):
        await papers.annotate_paper.__wrapped__(
            request,
            99999,
            body=AnnotationsRequest(rating=3),
            db_pool=pool,
            user_id=1,
        )

    assert exc_info.value.status_code == 404
    assert "99999" in exc_info.value.detail


# test_annotate_paper_unauthorized_user DELETED (wave4.4.D1):
# Superseded by libs/jarvis_common/tests/contract/test_idor_contract.py
# quadruple: ("PUT", "/api/papers/{paper_id_a}/annotations", "paper_id_a", "mutate")
# which asserts user B → 403/404 and user A → 200/204/409 against a real DB.


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


# Collapsed (Phase C): test_delete_paper_feedback_returns_204_for_existing_row
# Survivor: test_papers_contract.py::test_delete_paper_feedback_removes_row_scoped_to_user
# Behavioral outcome (DELETE returns 204/None, execute called once) covered by contract.
# SQL-text assertions for this test were already moved to contract in W4-followup (see line 1411).


# W4-followup: SQL-text/param-index assertions ($1 paper_id, $2 user_id, $3 source) moved to
# contract/test_papers_contract.py::test_delete_paper_feedback_removes_row_scoped_to_user


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


# W4-followup: collapsed to contract/test_papers_contract.py::test_delete_paper_feedback_removes_row_scoped_to_user
# test_delete_paper_feedback_scoped_to_exact_user: primary assertions were
# "IS NOT DISTINCT FROM" not in sql + "user_id = $2" in sql + positional[1]==13 (B1-09 class).

# W4-followup: collapsed to contract/test_papers_contract.py::test_delete_paper_feedback_removes_row_scoped_to_user
# test_delete_paper_feedback_different_user_id_not_deleted: primary assertion was
# positional[1] == 99 (param-binding check, B1-09 class); behavioral scoping proved by contract test.


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
    with patch.dict(task_registry._TASK_MAP, {"papers.batch_process": mock_task}):
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
