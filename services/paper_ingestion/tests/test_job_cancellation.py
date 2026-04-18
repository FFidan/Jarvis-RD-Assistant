"""Tests for job cancellation — paper_ingestion service.

Verifies that:
- A long-running job handler that honours ctx.is_cancelled() exits cleanly.
- After DELETE /api/jobs/{id} (i.e. request_cancel), the job row transitions
  to 'cancelled'.
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

_JOB_KIND = "test.cancellable"


def _make_pool(
    *,
    job_id: str,
    cancel_after: int = 1,
) -> tuple[MagicMock, list[dict]]:
    """Return a mock asyncpg pool that simulates a cancellable job row.

    The mock:
    - Returns a "queued → running" transition row on the first fetchrow call.
    - Returns cancel_requested=True after ``cancel_after`` is_cancelled polls.
    - Tracks all execute() calls so tests can assert final status.
    """
    execute_calls: list[dict] = []
    poll_count = 0

    async def _fetchrow(sql: str, *args, **kwargs):
        nonlocal poll_count
        # Atomic queued→running row (returned by run_job's UPDATE … RETURNING)
        if "status = 'queued'" in sql:
            return {"kind": _JOB_KIND, "payload": {}}
        # is_cancelled poll
        if "cancel_requested" in sql:
            poll_count += 1
            return {"cancel_requested": poll_count >= cancel_after}
        return None

    async def _execute(sql: str, *args, **kwargs):
        execute_calls.append({"sql": sql, "args": args})

    async def _executemany(sql: str, args_seq, **kwargs):
        for args in args_seq:
            execute_calls.append({"sql": sql, "args": args})

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=_fetchrow)
    conn.execute = AsyncMock(side_effect=_execute)
    conn.executemany = AsyncMock(side_effect=_executemany)

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
    """Register (and clean up) the test handler for the duration of each test."""
    from jarvis_common.jobs import job_handler

    @job_handler(_JOB_KIND)
    async def _cancellable_handler(pool, http_client, payload, ctx: JobContext):
        """Sleep in a loop, checking for cancellation on each iteration."""
        for _ in range(20):
            if await ctx.is_cancelled():
                raise asyncio.CancelledError
            await asyncio.sleep(0.01)
        return {"done": True}

    yield
    _HANDLERS.pop(_JOB_KIND, None)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancelled_job_reaches_cancelled_status() -> None:
    """A job that raises CancelledError is persisted with status='cancelled'."""
    job_id = str(uuid.uuid4())
    pool, calls = _make_pool(job_id=job_id, cancel_after=1)

    http_mock = AsyncMock()
    await run_job(pool, http_mock, job_id)

    # The final UPDATE should set status='cancelled'
    status_updates = [
        c for c in calls if "SET status" in c["sql"] and c["args"] and c["args"][0] == "cancelled"
    ]
    assert status_updates, (
        f"Expected a status='cancelled' UPDATE but got: {[c['sql'] for c in calls]}"
    )


@pytest.mark.asyncio
async def test_handler_exits_cleanly_on_cancellation() -> None:
    """run_job returns without raising when the handler is cancelled."""
    job_id = str(uuid.uuid4())
    pool, _ = _make_pool(job_id=job_id, cancel_after=1)
    http_mock = AsyncMock()

    # Should complete without raising
    await run_job(pool, http_mock, job_id)


@pytest.mark.asyncio
async def test_already_picked_up_job_is_silently_skipped() -> None:
    """run_job silently returns when the job is no longer in 'queued' state."""
    job_id = str(uuid.uuid4())

    async def _fetchrow_no_row(sql, *args, **kwargs):
        # Simulate the UPDATE … WHERE status='queued' returning nothing
        if "status = 'queued'" in sql:
            return None
        return {"cancel_requested": False}

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=_fetchrow_no_row)
    conn.execute = AsyncMock()

    ctx_mgr = MagicMock()
    ctx_mgr.__aenter__ = AsyncMock(return_value=conn)
    ctx_mgr.__aexit__ = AsyncMock(return_value=False)

    pool = MagicMock()
    pool.acquire.return_value = ctx_mgr

    http_mock = AsyncMock()
    await run_job(pool, http_mock, job_id)  # must not raise


@pytest.mark.asyncio
async def test_is_cancelled_returns_true_after_flag_set() -> None:
    """JobContext.is_cancelled() returns True once cancel_requested=TRUE in DB."""
    job_id = str(uuid.uuid4())

    call_count = 0

    async def _fetchrow(sql, *args, **kwargs):
        nonlocal call_count
        call_count += 1
        return {"cancel_requested": call_count >= 2}

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=_fetchrow)
    ctx_mgr = MagicMock()
    ctx_mgr.__aenter__ = AsyncMock(return_value=conn)
    ctx_mgr.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx_mgr

    ctx = JobContext(job_id=job_id, _pool=pool)
    assert not await ctx.is_cancelled()  # first call: cancel_requested=False
    assert await ctx.is_cancelled()  # second call: cancel_requested=True
    # Cached — no more DB polls
    assert await ctx.is_cancelled()
    assert conn.fetchrow.call_count == 2
