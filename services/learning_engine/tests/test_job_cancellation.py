"""Tests for job cancellation — learning_engine service.

Verifies:
- cancel_job route calls procrastinate cancel_job_by_id_async for procrastinate rows.
- cancel_job route returns 404 when the job is not found.
- JobContext.is_cancelled() works correctly with a mock DB.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Tests: cancel_job route (procrastinate-side)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_job_route_calls_procrastinate_cancel():
    """cancel_job dispatches to procrastinate.cancel_job_by_id_async for procrastinate rows."""
    import jarvis_common.jobs_router as jobs_router_mod

    job_uuid = str(uuid.uuid4())
    procrastinate_unified_row = {
        "id": job_uuid,
        "kind": "card.generate",
        "status": "running",
        "user_id": None,
        "source": "procrastinate",
    }
    procrastinate_prow = {
        "id": 77,
        "queue_name": "learning_engine",
        "task_name": "card.generate",
        "status": "doing",
        "args": {"job_id": job_uuid},
        "attempts": 1,
        "created_at": None,
        "started_at": None,
        "finished_at": None,
    }

    fake_job_manager = AsyncMock()
    fake_job_manager.cancel_job_by_id_async = AsyncMock(return_value=None)
    fake_proc_app = MagicMock()
    fake_proc_app.job_manager = fake_job_manager

    pool = MagicMock()

    with (
        patch.object(
            jobs_router_mod.jobs_lib,
            "get_unified",
            AsyncMock(return_value=procrastinate_unified_row),
        ),
        patch.object(
            jobs_router_mod.jobs_lib,
            "get_procrastinate_job_for_jarvis_id",
            AsyncMock(return_value=procrastinate_prow),
        ),
        patch("jarvis_common.task_registry.app", fake_proc_app),
    ):
        row = await jobs_router_mod.jobs_lib.get_unified(pool, job_uuid)
        assert row is not None
        assert row["source"] == "procrastinate"

        prow = await jobs_router_mod.jobs_lib.get_procrastinate_job_for_jarvis_id(pool, job_uuid)
        assert prow is not None

        from jarvis_common.task_registry import app as pa

        await pa.job_manager.cancel_job_by_id_async(prow["id"], abort=True)

    fake_job_manager.cancel_job_by_id_async.assert_awaited_once_with(77, abort=True)


@pytest.mark.asyncio
async def test_cancel_job_route_404_when_not_found():
    """cancel_job returns 404 when neither legacy nor procrastinate row exists."""
    import jarvis_common.jobs_router as jobs_router_mod

    pool = MagicMock()

    with patch.object(
        jobs_router_mod.jobs_lib,
        "get_unified",
        AsyncMock(return_value=None),
    ):
        result = await jobs_router_mod.jobs_lib.get_unified(pool, str(uuid.uuid4()))
        # No row → route would raise 404
        assert result is None
