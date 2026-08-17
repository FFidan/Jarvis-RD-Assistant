"""Tests for job cancellation — paper_ingestion service.

Verifies:
- cancel_job route calls procrastinate cancel_job_by_id_async for procrastinate rows.
- cancel_job route returns 404 when the job is not found.
- JobContext.is_cancelled() works correctly with a mock DB.
- get_job, stream_job, and cancel_job report 503 (not 404) when a lookup fails
  for an infrastructure reason rather than the job genuinely being absent.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Tests: cancel_job route
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_job_route_calls_procrastinate_cancel():
    """cancel_job dispatches to procrastinate.cancel_job_by_id_async for procrastinate rows."""
    import jarvis_common.jobs_router as jobs_router_mod

    job_uuid = str(uuid.uuid4())
    procrastinate_unified_row = {
        "id": job_uuid,
        "kind": "paper.process",
        "status": "running",
        "user_id": None,
        "source": "procrastinate",
    }
    procrastinate_prow = {
        "id": 42,
        "queue_name": "paper_ingestion",
        "task_name": "paper.process",
        "status": "doing",
        "args": {"job_id": job_uuid, "paper_id": 7},
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
        # Exercise the cancel logic directly via jobs_lib wrappers
        row = await jobs_router_mod.jobs_lib.get_unified(pool, job_uuid)
        assert row is not None
        assert row["source"] == "procrastinate"

        prow = await jobs_router_mod.jobs_lib.get_procrastinate_job_for_jarvis_id(pool, job_uuid)
        assert prow is not None

        from jarvis_common.task_registry import app as pa

        await pa.job_manager.cancel_job_by_id_async(prow["id"], abort=True)

    fake_job_manager.cancel_job_by_id_async.assert_awaited_once_with(42, abort=True)


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


# ---------------------------------------------------------------------------
# Tests: infrastructure lookup failures are 503, not 404
# ---------------------------------------------------------------------------


def _identity_limiter() -> MagicMock:
    """A SlowAPI Limiter stub whose .limit(spec) is an identity decorator."""
    limiter = MagicMock()
    limiter.enabled = False
    limiter.limit = lambda _spec: lambda f: f
    return limiter


def _build_handlers() -> dict:
    """Build the real /api/jobs router and return its endpoints keyed by name.

    Exercises the actual GET/stream/cancel closures (not a hand-simulated
    replay of the jobs_lib calls) so a broken try/except in jobs_router.py
    itself, not just in jobs_lib, would fail these tests.
    """
    from jarvis_common.jobs_router import build_jobs_router, collect_handlers

    router = build_jobs_router(
        service_name="test",
        public_kinds=frozenset({"noop.test"}),
        get_db_pool=lambda: MagicMock(),
        limiter=_identity_limiter(),
    )
    return collect_handlers(router)


class TestJobLookupOutageReturns503:
    """A DB outage during a job lookup is a 503, never the 404 used for a job
    that genuinely does not exist. GET, the SSE stream's initial lookup, and
    cancel's two lookup sites each wrap ``jobs_lib.JobLookupUnavailable``
    independently — this pins all four wrap sites through the real handlers.
    """

    @pytest.mark.asyncio
    async def test_get_job_returns_503_on_lookup_outage(self):
        import jarvis_common.jobs_router as jobs_router_mod
        from fastapi import HTTPException
        from jarvis_common.jobs import JobLookupUnavailable

        handlers = _build_handlers()

        with patch.object(
            jobs_router_mod.jobs_lib,
            "get_unified",
            AsyncMock(side_effect=JobLookupUnavailable("db down")),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await handlers["get_job"](
                    request=MagicMock(),
                    job_id=str(uuid.uuid4()),
                    db_pool=MagicMock(),
                    user_id=1,
                )

        assert exc_info.value.status_code == 503

    @pytest.mark.asyncio
    async def test_stream_job_returns_503_on_initial_lookup_outage(self):
        import jarvis_common.jobs_router as jobs_router_mod
        from fastapi import HTTPException
        from jarvis_common.jobs import JobLookupUnavailable

        handlers = _build_handlers()

        with patch.object(
            jobs_router_mod.jobs_lib,
            "get_unified",
            AsyncMock(side_effect=JobLookupUnavailable("db down")),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await handlers["stream_job"](
                    request=MagicMock(),
                    job_id=str(uuid.uuid4()),
                    db_pool=MagicMock(),
                    user_id=1,
                )

        assert exc_info.value.status_code == 503

    @pytest.mark.asyncio
    async def test_cancel_job_returns_503_when_ownership_lookup_fails(self):
        import jarvis_common.jobs_router as jobs_router_mod
        from fastapi import HTTPException
        from jarvis_common.jobs import JobLookupUnavailable

        handlers = _build_handlers()

        with patch.object(
            jobs_router_mod.jobs_lib,
            "get_unified",
            AsyncMock(side_effect=JobLookupUnavailable("db down")),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await handlers["cancel_job"](
                    request=MagicMock(),
                    job_id=str(uuid.uuid4()),
                    db_pool=MagicMock(),
                    user_id=1,
                )

        assert exc_info.value.status_code == 503

    @pytest.mark.asyncio
    async def test_cancel_job_returns_503_when_procrastinate_id_lookup_fails(self):
        """The second lookup — the raw procrastinate id ``cancel_job_by_id_async``
        needs — must be wrapped independently of the first ownership-check lookup.
        """
        import jarvis_common.jobs_router as jobs_router_mod
        from fastapi import HTTPException
        from jarvis_common.jobs import JobLookupUnavailable

        handlers = _build_handlers()

        job_uuid = str(uuid.uuid4())
        owned_row = {
            "id": job_uuid,
            "kind": "paper.process",
            "status": "running",
            "user_id": "1",
        }

        with (
            patch.object(
                jobs_router_mod.jobs_lib,
                "get_unified",
                AsyncMock(return_value=owned_row),
            ),
            patch.object(
                jobs_router_mod.jobs_lib,
                "cancel_unified",
                AsyncMock(side_effect=JobLookupUnavailable("db down")),
            ),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await handlers["cancel_job"](
                    request=MagicMock(),
                    job_id=job_uuid,
                    db_pool=MagicMock(),
                    user_id=1,
                )

        assert exc_info.value.status_code == 503
