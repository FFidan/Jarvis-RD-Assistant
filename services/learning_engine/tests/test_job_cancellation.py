"""Tests for job cancellation — learning_engine service.

Verifies that:
- A long-running job handler that honours ctx.is_cancelled() exits cleanly.
- After a cancel is requested the job row transitions to 'cancelled'.
"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from jarvis_common.jobs import _HANDLERS, JobContext, run_job

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_JOB_KIND = "le.test.cancellable"


def _make_pool(*, cancel_after: int = 1) -> tuple[MagicMock, list[dict]]:
    """Return a mock asyncpg pool that simulates a cancellable job.

    Parameters
    ----------
    cancel_after:
        Return cancel_requested=True after this many is_cancelled polls.
    """
    execute_calls: list[dict] = []
    poll_count = 0

    async def _fetchrow(sql: str, *args, **kwargs):
        nonlocal poll_count
        if "status = 'queued'" in sql:
            return {"kind": _JOB_KIND, "payload": {}}
        if "cancel_requested" in sql:
            poll_count += 1
            return {"cancel_requested": poll_count >= cancel_after}
        return None

    async def _execute(sql: str, *args, **kwargs):
        execute_calls.append({"sql": sql, "args": args})

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=_fetchrow)
    conn.execute = AsyncMock(side_effect=_execute)

    ctx_mgr = MagicMock()
    ctx_mgr.__aenter__ = AsyncMock(return_value=conn)
    ctx_mgr.__aexit__ = AsyncMock(return_value=False)

    pool = MagicMock()
    pool.acquire.return_value = ctx_mgr
    return pool, execute_calls


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _register_handler():
    """Register and clean up the test handler for the duration of each test."""
    from jarvis_common.jobs import job_handler

    @job_handler(_JOB_KIND)
    async def _long_running(pool, http_client, payload, ctx: JobContext):
        for _ in range(20):
            if await ctx.is_cancelled():
                raise asyncio.CancelledError
            await asyncio.sleep(0.01)
        return {"ok": True}

    yield
    _HANDLERS.pop(_JOB_KIND, None)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancelled_job_status_transitions_to_cancelled() -> None:
    """Handler that raises CancelledError causes job status to become 'cancelled'."""
    job_id = str(uuid.uuid4())
    pool, calls = _make_pool(cancel_after=1)
    http_mock = AsyncMock()

    await run_job(pool, http_mock, job_id)

    cancelled_updates = [
        c for c in calls if "SET status" in c["sql"] and c["args"] and c["args"][0] == "cancelled"
    ]
    assert cancelled_updates, (
        f"Expected status='cancelled' in DB calls, got: {[c['sql'] for c in calls]}"
    )


@pytest.mark.asyncio
async def test_run_job_does_not_raise_on_cancellation() -> None:
    """run_job completes without propagating CancelledError to the caller."""
    job_id = str(uuid.uuid4())
    pool, _ = _make_pool(cancel_after=1)
    http_mock = AsyncMock()

    await run_job(pool, http_mock, job_id)  # must not raise


@pytest.mark.asyncio
async def test_is_cancelled_caches_true_result() -> None:
    """Once is_cancelled returns True it caches and stops polling the DB."""
    job_id = str(uuid.uuid4())
    poll_count = 0

    async def _fetchrow(sql, *args, **kwargs):
        nonlocal poll_count
        poll_count += 1
        return {"cancel_requested": poll_count >= 1}

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=_fetchrow)
    ctx_mgr = MagicMock()
    ctx_mgr.__aenter__ = AsyncMock(return_value=conn)
    ctx_mgr.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx_mgr

    ctx = JobContext(job_id=job_id, _pool=pool)
    assert await ctx.is_cancelled()  # first poll → True
    assert await ctx.is_cancelled()  # cached → no extra poll
    assert await ctx.is_cancelled()  # cached → no extra poll
    assert poll_count == 1, f"Expected 1 DB poll but got {poll_count}"


@pytest.mark.asyncio
async def test_is_cancelled_returns_false_before_flag_set() -> None:
    """is_cancelled returns False when cancel_requested is still FALSE in DB."""
    job_id = str(uuid.uuid4())

    async def _fetchrow(sql, *args, **kwargs):
        return {"cancel_requested": False}

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=_fetchrow)
    ctx_mgr = MagicMock()
    ctx_mgr.__aenter__ = AsyncMock(return_value=conn)
    ctx_mgr.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx_mgr

    ctx = JobContext(job_id=job_id, _pool=pool)
    assert not await ctx.is_cancelled()
    assert not await ctx.is_cancelled()
