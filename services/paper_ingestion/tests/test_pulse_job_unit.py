"""Unit tests for pulse/job.py helpers (no real DB required).

Covers: _emit_post_run_telemetry must call defer_async for
pulse.train_classifier regardless of whether ctx is present.

Verified identifiers:
  pulse.job._emit_post_run_telemetry  job.py:502 — async helper, Stage 9
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_emit_post_run_telemetry_calls_defer_async_without_ctx():
    """_emit_post_run_telemetry enqueues pulse.train_classifier even when ctx=None.

    The defer_async call must be outside `if ctx:`, so passing ctx=None does not
    silently skip classifier-training enqueue.  This test pins the corrected
    behaviour: defer_async MUST be called regardless of ctx.

    Verified: pulse/job.py:510-522 (defer_async outside ctx guard).
    """
    from paper_ingestion.pulse.job import _emit_post_run_telemetry

    mock_task = MagicMock()
    mock_task.defer_async = AsyncMock(return_value=None)

    stats: dict = {}

    with (
        patch(
            "paper_ingestion.pulse.job.KIND_TO_TASK",
            {"pulse.train_classifier": mock_task},
        ),
        patch(
            "paper_ingestion.pulse.job.log_event",
            AsyncMock(return_value=None),
        ),
    ):
        await _emit_post_run_telemetry(
            db_pool=MagicMock(),
            ctx=None,
            stage2_out=[],
            stats=stats,
            user_id=42,
        )

    mock_task.defer_async.assert_called_once()
    call_kwargs = mock_task.defer_async.call_args.kwargs
    assert "job_id" in call_kwargs, "defer_async must be called with job_id kwarg"
    assert call_kwargs["user_id"] == 42
    assert stats.get("classifier_training_enqueued") is True


@pytest.mark.asyncio
async def test_emit_post_run_telemetry_calls_update_progress_when_ctx_present():
    """_emit_post_run_telemetry calls ctx.update_progress when ctx is present.

    Companion to test_emit_post_run_telemetry_calls_defer_async_without_ctx.
    Verifies that when ctx is provided, update_progress is called with the
    correct arguments (progress=1.0, message="Done"), and defer_async STILL
    fires (proving the hoist of defer_async outside the ctx guard works).

    Verified: pulse/job.py:514-524 (defer_async outside, ctx.update_progress inside).
    """
    from paper_ingestion.pulse.job import _emit_post_run_telemetry

    mock_task = MagicMock()
    mock_task.defer_async = AsyncMock(return_value=None)

    ctx = AsyncMock()
    stats: dict = {}

    with (
        patch(
            "paper_ingestion.pulse.job.KIND_TO_TASK",
            {"pulse.train_classifier": mock_task},
        ),
        patch(
            "paper_ingestion.pulse.job.log_event",
            AsyncMock(return_value=None),
        ),
    ):
        await _emit_post_run_telemetry(
            db_pool=MagicMock(),
            ctx=ctx,
            stage2_out=[],
            stats=stats,
            user_id=42,
        )

    # Verify defer_async was called (hoist outside ctx guard works)
    mock_task.defer_async.assert_called_once()
    call_kwargs = mock_task.defer_async.call_args.kwargs
    assert call_kwargs["user_id"] == 42
    assert stats.get("classifier_training_enqueued") is True

    # Verify ctx.update_progress was called with correct args
    ctx.update_progress.assert_called_once_with(1.0, "Done")


def _make_unscored_candidate():
    """A stage-1 survivor with no LLM scores (the all-fail per-card outcome)."""
    from datetime import date

    from paper_ingestion.models import PaperCreate, SourceType
    from paper_ingestion.pulse.scoring import ScoredCandidate

    paper = PaperCreate(
        external_id="arxiv:degr-0001",
        source_type=SourceType.ARXIV,
        title="Degraded Paper",
        authors=["Author A"],
        abstract="Abstract.",
        published_date=date(2025, 1, 1),
        url="https://arxiv.org/abs/degr-0001",
    )
    return ScoredCandidate(
        paper=paper,
        signals={"embedding": 0.7, "recency": 0.5},
        llm_relevance=None,
        llm_novelty=None,
        reasoning="LLM scoring failed",
        final_score=0.6,
    )


@pytest.mark.asyncio
async def test_run_stage2_sets_degraded_reason_when_all_cards_fail():
    """All per-card stage-2 calls failing must surface a deck-level degraded_reason.

    stage2_llm_rerank catches per-card failures internally and returns cards with
    llm_relevance=None. Without a deck-level reason the UI renders "AI scoring
    unavailable" cards with no banner — the bug this guards. _run_stage2 must
    detect the all-None outcome and set degraded_reason while still returning the
    cards (embedding-only ranking remains usable).

    Verified: pulse/job.py:_run_stage2 — all-fail degradation branch.
    """
    from paper_ingestion.pulse.job import _run_stage2

    failed = [_make_unscored_candidate() for _ in range(5)]

    services = MagicMock()
    services.verifier = MagicMock()
    services.openai_client = object()

    with (
        patch("paper_ingestion.pulse.job.get_services", return_value=services),
        patch(
            "paper_ingestion.pulse.job.effective_num_ctx",
            AsyncMock(return_value=4096),
        ),
        patch(
            "paper_ingestion.pulse.job.stage2_llm_rerank",
            AsyncMock(return_value=failed),
        ),
    ):
        stage2_out, degraded_reason, llm_calls = await _run_stage2(
            failed, profile=MagicMock(), ctx=None, db_pool=MagicMock()
        )

    assert llm_calls == 0
    assert len(stage2_out) == 5
    assert degraded_reason, "all-fail stage-2 must set a non-null deck degraded_reason"
    assert all(sc.llm_relevance is None for sc in stage2_out)
