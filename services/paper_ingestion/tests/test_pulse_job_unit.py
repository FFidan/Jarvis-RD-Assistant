"""Unit tests for pulse/job.py helpers (no real DB required).

Covers the W6-T3 fix: _emit_post_run_telemetry must call defer_async for
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

    Before the W6-T3 fix the defer_async call was inside `if ctx:`, so passing
    ctx=None silently skipped classifier-training enqueue.  This test pins the
    corrected behaviour: defer_async MUST be called regardless of ctx.

    Verified: pulse/job.py:510-522 (defer_async outside ctx guard after W6-T3 fix).
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
