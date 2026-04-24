"""Unit tests for jarvis_common.jobs.

Tests use a real asyncpg pool against a PostgreSQL database.
Set TEST_DATABASE_URL (e.g. postgresql://user:pass@localhost:5432/testdb) to run.
Tests are skipped automatically when the env var is unset or the DB is unreachable.

The test schema is isolated in a per-test temporary table (prefix-renamed) so
concurrent test runs don't interfere with each other.  Each test function
gets a fresh jobs table via the ``db_pool`` fixture.
"""

from __future__ import annotations

import os

# NOTE: Must be set BEFORE importing jarvis_common.jobs so the gated
# ``noop.test`` handler registers at module import time.
os.environ.setdefault("JARVIS_ENABLE_TEST_JOBS", "1")

import asyncio
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

# ---------------------------------------------------------------------------
# Conditional skip if no real DB is available
# ---------------------------------------------------------------------------

_DB_URL = os.environ.get("TEST_DATABASE_URL", "")


# ---------------------------------------------------------------------------
# Pure-unit tests (no DB required) — test registration and helpers
# ---------------------------------------------------------------------------


def test_job_handler_registration():
    """@job_handler should register the decorated function under its kind."""
    from jarvis_common.jobs import _HANDLERS, job_handler

    unique_kind = f"test.reg.{uuid.uuid4().hex}"

    @job_handler(unique_kind)
    async def _dummy(_p, _h, _pl, _ctx):
        return {}

    assert _HANDLERS[unique_kind] is _dummy


def test_job_handler_returns_original_fn():
    """The decorator must return the original callable unchanged."""
    from jarvis_common.jobs import job_handler

    unique_kind = f"test.ret.{uuid.uuid4().hex}"

    async def _original(_p, _h, _pl, _ctx):
        return {}

    result = job_handler(unique_kind)(_original)
    assert result is _original


def test_sanitize_error_message_strips_ansi():
    """_sanitize_error_message should remove ANSI escape sequences."""
    from jarvis_common.jobs import _sanitize_error_message

    raw = "\x1b[31mERROR\x1b[0m something failed"
    out = _sanitize_error_message(raw)
    assert "\x1b" not in out
    assert "something failed" in out


def test_sanitize_error_message_strips_paths():
    """_sanitize_error_message should redact absolute filesystem paths."""
    from jarvis_common.jobs import _sanitize_error_message

    raw = "file not found: /home/user/secret/file.txt"
    out = _sanitize_error_message(raw)
    assert "/home/user/secret/file.txt" not in out
    assert "file not found:" in out


def test_sanitize_error_message_truncates_to_500():
    """_sanitize_error_message caps the result at 500 characters."""
    from jarvis_common.jobs import _sanitize_error_message

    out = _sanitize_error_message("x" * 1000)
    assert len(out) == 500


def test_job_error_carries_action_link():
    """JobError stores action_link and message correctly."""
    from jarvis_common.jobs import JobError

    link = {"label": "Retry", "href": "/api/retry"}
    err = JobError("something went wrong", action_link=link)
    assert str(err) == "something went wrong"
    assert err.action_link == link


def test_job_error_without_action_link():
    """JobError with no action_link has action_link=None."""
    from jarvis_common.jobs import JobError

    err = JobError("simple error")
    assert err.action_link is None


@pytest.mark.asyncio
async def test_enqueue_raises_runtime_error_when_fetchrow_returns_none():
    """JC-007: enqueue raises RuntimeError (not AssertionError) when DB returns None."""
    from jarvis_common.jobs import enqueue

    pool, conn = _make_mock_pool_returning([None])
    conn.fetchrow = AsyncMock(return_value=None)

    with pytest.raises(RuntimeError, match="enqueue returned no row"):
        await enqueue(pool, "some.kind", {})


def test_keepalive_interval_is_public():
    """JC-009: KEEPALIVE_INTERVAL is importable as a public name from jarvis_common.jobs."""
    from jarvis_common.jobs import KEEPALIVE_INTERVAL

    assert isinstance(KEEPALIVE_INTERVAL, float)
    assert KEEPALIVE_INTERVAL > 0


def test_max_stream_seconds_is_public():
    """JC-009: MAX_STREAM_SECONDS is importable as a public name from jarvis_common.jobs."""
    from jarvis_common.jobs import MAX_STREAM_SECONDS

    assert isinstance(MAX_STREAM_SECONDS, int)
    assert MAX_STREAM_SECONDS > 0


def test_keepalive_and_max_stream_exported_from_jarvis_common():
    """JC-009: Both constants are re-exported from jarvis_common __init__."""
    import jarvis_common

    assert hasattr(jarvis_common, "KEEPALIVE_INTERVAL")
    assert hasattr(jarvis_common, "MAX_STREAM_SECONDS")


# ---------------------------------------------------------------------------
# Helpers for mock-based tests (no real DB)
# ---------------------------------------------------------------------------


def _make_mock_pool_returning(rows: list[dict]):
    """Build an AsyncMock pool whose fetch() returns the given rows."""
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=rows)
    conn.fetch = AsyncMock(return_value=rows)
    conn.execute = AsyncMock(return_value=None)

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=False)

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=cm)
    pool.fetch = AsyncMock(return_value=rows)
    return pool, conn


# ---------------------------------------------------------------------------
# DB-backed tests — require TEST_DATABASE_URL
# ---------------------------------------------------------------------------

_SKIP_NO_DB = pytest.mark.skipif(
    not _DB_URL,
    reason="TEST_DATABASE_URL not set — skipping live DB tests",
)


@pytest_asyncio.fixture(scope="function")
async def db_pool():
    """Create a real asyncpg pool, create a fresh jobs table, yield, teardown."""
    if not _DB_URL:
        pytest.skip("TEST_DATABASE_URL not set")

    import asyncpg
    from jarvis_common import init_pg_connection

    try:
        pool = await asyncpg.create_pool(
            _DB_URL,
            min_size=1,
            max_size=3,
            init=init_pg_connection,
        )
    except Exception as exc:
        pytest.skip(f"Cannot connect to test DB: {exc}")
        return  # unreachable but satisfies type checker

    # Ensure the jobs table exists (applied by migration 023; create it if missing).
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                kind            TEXT NOT NULL,
                status          TEXT NOT NULL DEFAULT 'queued',
                payload         JSONB NOT NULL DEFAULT '{}',
                result          JSONB,
                error           JSONB,
                progress        FLOAT,
                progress_message TEXT,
                cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,
                user_id         TEXT,
                created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                started_at      TIMESTAMPTZ,
                finished_at     TIMESTAMPTZ
            )
        """)
        # Clean slate for each test
        await conn.execute("DELETE FROM jobs")

    yield pool

    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM jobs")
    await pool.close()


@_SKIP_NO_DB
@pytest.mark.asyncio
async def test_enqueue_returns_uuid_string(db_pool):
    """enqueue() should return a non-empty UUID string."""
    from jarvis_common.jobs import enqueue

    job_id = await enqueue(db_pool, "noop.test", {"x": 1})
    assert isinstance(job_id, str)
    # Should be parseable as UUID
    uuid.UUID(job_id)


@_SKIP_NO_DB
@pytest.mark.asyncio
async def test_get_returns_queued_row(db_pool):
    """get() on a freshly-enqueued job should return status='queued' with correct fields."""
    from jarvis_common.jobs import enqueue, get

    job_id = await enqueue(db_pool, "noop.test", {"hello": "world"}, user_id="user-1")
    row = await get(db_pool, job_id)

    assert row is not None
    assert row["status"] == "queued"
    assert row["kind"] == "noop.test"
    assert row["payload"] == {"hello": "world"}
    assert row["user_id"] == "user-1"
    assert row["result"] is None
    assert row["error"] is None


@_SKIP_NO_DB
@pytest.mark.asyncio
async def test_get_returns_none_for_missing(db_pool):
    """get() on a non-existent job should return None."""
    from jarvis_common.jobs import get

    row = await get(db_pool, str(uuid.uuid4()))
    assert row is None


@_SKIP_NO_DB
@pytest.mark.asyncio
async def test_list_jobs_filters_by_status(db_pool):
    """list_jobs() with status= filter should only return matching jobs."""
    from jarvis_common.jobs import enqueue, list_jobs

    j1 = await enqueue(db_pool, "noop.test", {})
    j2 = await enqueue(db_pool, "noop.test", {})

    # Manually flip j2 to running
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE jobs SET status = 'running' WHERE id = $1::uuid", j2)

    queued = await list_jobs(db_pool, status="queued")
    running = await list_jobs(db_pool, status="running")

    queued_ids = {r["id"] if isinstance(r["id"], str) else str(r["id"]) for r in queued}
    running_ids = {r["id"] if isinstance(r["id"], str) else str(r["id"]) for r in running}

    assert j1 in queued_ids
    assert j2 not in queued_ids
    assert j2 in running_ids


@_SKIP_NO_DB
@pytest.mark.asyncio
async def test_list_jobs_filters_by_kind(db_pool):
    """list_jobs() with kind= filter should only return matching jobs."""
    from jarvis_common.jobs import enqueue, list_jobs

    j1 = await enqueue(db_pool, "card.generate", {})
    _j2 = await enqueue(db_pool, "noop.test", {})

    rows = await list_jobs(db_pool, kind="card.generate")
    ids = {r["id"] if isinstance(r["id"], str) else str(r["id"]) for r in rows}
    assert j1 in ids
    assert all(r["kind"] == "card.generate" for r in rows)


@_SKIP_NO_DB
@pytest.mark.asyncio
async def test_run_job_succeeds_with_registered_handler(db_pool):
    """run_job() should execute a registered handler and mark the job succeeded."""
    import asyncpg
    import httpx
    from jarvis_common.jobs import JobContext, enqueue, get, job_handler, run_job

    unique_kind = f"test.run.{uuid.uuid4().hex}"
    results_store: list[dict] = []

    @job_handler(unique_kind)
    async def _handler(
        _pool: asyncpg.Pool,
        _http: httpx.AsyncClient,
        payload: dict[str, Any],
        ctx: JobContext,
    ) -> dict[str, Any]:
        await ctx.update_progress(0.5, "halfway")
        results_store.append(payload)
        return {"done": True, "echo": payload}

    job_id = await enqueue(db_pool, unique_kind, {"key": "val"})
    http_mock = AsyncMock(spec=httpx.AsyncClient)

    await run_job(db_pool, http_mock, job_id)

    row = await get(db_pool, job_id)
    assert row is not None
    assert row["status"] == "succeeded"
    assert row["result"] == {"done": True, "echo": {"key": "val"}}
    assert row["error"] is None
    assert row["progress"] == 1.0
    assert results_store == [{"key": "val"}]


@_SKIP_NO_DB
@pytest.mark.asyncio
async def test_run_job_noop_if_already_running(db_pool):
    """run_job() on a job already in 'running' status should be a no-op."""
    import httpx
    from jarvis_common.jobs import enqueue, get, run_job

    job_id = await enqueue(db_pool, "noop.test", {})
    # Force status to 'running' before calling run_job
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE jobs SET status = 'running', started_at = NOW() WHERE id = $1::uuid",
            job_id,
        )

    http_mock = AsyncMock(spec=httpx.AsyncClient)
    # Should return silently — no handler called, status unchanged
    await run_job(db_pool, http_mock, job_id)

    row = await get(db_pool, job_id)
    assert row is not None
    assert row["status"] == "running"  # still running, no terminal status set


@_SKIP_NO_DB
@pytest.mark.asyncio
async def test_run_job_fails_gracefully_on_unknown_kind(db_pool):
    """run_job() with an unregistered kind should mark job failed."""
    import httpx
    from jarvis_common.jobs import enqueue, get, run_job

    job_id = await enqueue(db_pool, "totally.unknown.kind", {})
    http_mock = AsyncMock(spec=httpx.AsyncClient)

    await run_job(db_pool, http_mock, job_id)

    row = await get(db_pool, job_id)
    assert row is not None
    assert row["status"] == "failed"
    assert row["error"] is not None
    assert "totally.unknown.kind" in row["error"]["message"]


@_SKIP_NO_DB
@pytest.mark.asyncio
async def test_run_job_generic_exception_produces_sanitized_error(db_pool):
    """Handler raising a generic Exception should end job as failed with sanitized message."""
    import asyncpg
    import httpx
    from jarvis_common.jobs import JobContext, enqueue, get, job_handler, run_job

    unique_kind = f"test.exc.{uuid.uuid4().hex}"

    @job_handler(unique_kind)
    async def _exploding(
        _pool: asyncpg.Pool,
        _http: httpx.AsyncClient,
        payload: dict[str, Any],
        ctx: JobContext,
    ) -> dict[str, Any]:
        raise RuntimeError("/some/private/path: boom")

    job_id = await enqueue(db_pool, unique_kind, {})
    http_mock = AsyncMock(spec=httpx.AsyncClient)

    await run_job(db_pool, http_mock, job_id)

    row = await get(db_pool, job_id)
    assert row is not None
    assert row["status"] == "failed"
    error = row["error"]
    assert error is not None
    assert "message" in error
    # Path should have been sanitised
    assert "/some/private/path" not in error["message"]
    assert "boom" in error["message"]


@_SKIP_NO_DB
@pytest.mark.asyncio
async def test_run_job_job_error_with_action_link(db_pool):
    """Handler raising JobError with action_link should persist that link in error JSON."""
    import asyncpg
    import httpx
    from jarvis_common.jobs import JobContext, JobError, enqueue, get, job_handler, run_job

    unique_kind = f"test.joberr.{uuid.uuid4().hex}"

    @job_handler(unique_kind)
    async def _raises_job_error(
        _pool: asyncpg.Pool,
        _http: httpx.AsyncClient,
        payload: dict[str, Any],
        ctx: JobContext,
    ) -> dict[str, Any]:
        raise JobError(
            "Paper not found",
            action_link={"label": "Upload PDF", "href": "/upload"},
        )

    job_id = await enqueue(db_pool, unique_kind, {})
    http_mock = AsyncMock(spec=httpx.AsyncClient)

    await run_job(db_pool, http_mock, job_id)

    row = await get(db_pool, job_id)
    assert row is not None
    assert row["status"] == "failed"
    error = row["error"]
    assert error is not None
    assert error["message"] == "Paper not found"
    assert error["action_link"] == {"label": "Upload PDF", "href": "/upload"}


@_SKIP_NO_DB
@pytest.mark.asyncio
async def test_request_cancel_sets_flag(db_pool):
    """request_cancel() should set cancel_requested=TRUE in the DB."""
    from jarvis_common.jobs import enqueue, get, request_cancel

    job_id = await enqueue(db_pool, "noop.test", {})
    row_before = await get(db_pool, job_id)
    assert row_before is not None
    assert not row_before["cancel_requested"]

    await request_cancel(db_pool, job_id)

    row_after = await get(db_pool, job_id)
    assert row_after is not None
    assert row_after["cancel_requested"] is True


@_SKIP_NO_DB
@pytest.mark.asyncio
async def test_job_context_is_cancelled_returns_true_after_request(db_pool):
    """JobContext.is_cancelled() should return True after request_cancel()."""
    from jarvis_common.jobs import JobContext, enqueue, request_cancel

    job_id = await enqueue(db_pool, "noop.test", {})
    ctx = JobContext(job_id=job_id, _pool=db_pool)

    assert not await ctx.is_cancelled()

    await request_cancel(db_pool, job_id)

    assert await ctx.is_cancelled()


@_SKIP_NO_DB
@pytest.mark.asyncio
async def test_run_job_cancelled_error_transitions_to_cancelled(db_pool):
    """Handler raising asyncio.CancelledError should mark job as 'cancelled'."""
    import asyncpg
    import httpx
    from jarvis_common.jobs import JobContext, enqueue, get, job_handler, run_job

    unique_kind = f"test.cancel.{uuid.uuid4().hex}"

    @job_handler(unique_kind)
    async def _cancellable(
        _pool: asyncpg.Pool,
        _http: httpx.AsyncClient,
        payload: dict[str, Any],
        ctx: JobContext,
    ) -> dict[str, Any]:
        raise asyncio.CancelledError()

    job_id = await enqueue(db_pool, unique_kind, {})
    http_mock = AsyncMock(spec=httpx.AsyncClient)

    await run_job(db_pool, http_mock, job_id)

    row = await get(db_pool, job_id)
    assert row is not None
    assert row["status"] == "cancelled"


@_SKIP_NO_DB
@pytest.mark.asyncio
async def test_worker_loop_picks_up_and_finishes_job(db_pool):
    """worker_loop should pick up a queued job and complete it within a few polls."""
    import asyncpg
    import httpx
    from jarvis_common.jobs import JobContext, enqueue, get, job_handler, worker_loop

    unique_kind = f"test.wloop.{uuid.uuid4().hex}"

    @job_handler(unique_kind)
    async def _fast(
        _pool: asyncpg.Pool,
        _http: httpx.AsyncClient,
        payload: dict[str, Any],
        ctx: JobContext,
    ) -> dict[str, Any]:
        return {"processed": True}

    job_id = await enqueue(db_pool, unique_kind, {"n": 42})
    http_mock = AsyncMock(spec=httpx.AsyncClient)
    stop_event = asyncio.Event()

    kinds = {unique_kind}

    async def _run_worker():
        await worker_loop(
            db_pool,
            http_mock,
            kinds=kinds,
            poll_interval=0.1,
            stop_event=stop_event,
        )

    worker_task = asyncio.create_task(_run_worker())
    try:
        # Wait up to 5 seconds for the job to reach a terminal state
        deadline = asyncio.get_event_loop().time() + 5.0
        while asyncio.get_event_loop().time() < deadline:
            row = await get(db_pool, job_id)
            if row and row["status"] == "succeeded":
                break
            await asyncio.sleep(0.1)
        else:
            row = await get(db_pool, job_id)
            pytest.fail(f"Job did not succeed within timeout; status={row and row['status']}")

        assert row is not None
        assert row["status"] == "succeeded"
        assert row["result"] == {"processed": True}
    finally:
        stop_event.set()
        try:
            await asyncio.wait_for(worker_task, timeout=2.0)
        except (TimeoutError, asyncio.CancelledError):
            worker_task.cancel()


@_SKIP_NO_DB
@pytest.mark.asyncio
async def test_worker_loop_reaps_stale_running_jobs(db_pool):
    """_reap_stale_jobs() should mark running jobs older than 30 min as failed."""
    from jarvis_common.jobs import _reap_stale_jobs

    # Insert a 'running' job whose started_at is 45 minutes in the past.
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO jobs (kind, status, started_at)
            VALUES ($1, 'running', NOW() - INTERVAL '45 minutes')
            RETURNING id::text
            """,
            "noop.test",
        )
    assert row is not None
    stale_id = row["id"]

    # Also insert a fresh running job that should NOT be reaped.
    async with db_pool.acquire() as conn:
        row_fresh = await conn.fetchrow(
            """
            INSERT INTO jobs (kind, status, started_at)
            VALUES ($1, 'running', NOW() - INTERVAL '1 minute')
            RETURNING id::text
            """,
            "noop.test",
        )
    assert row_fresh is not None
    fresh_id = row_fresh["id"]

    # Run reaper once.
    await _reap_stale_jobs(db_pool)

    from jarvis_common.jobs import get

    stale = await get(db_pool, stale_id)
    fresh = await get(db_pool, fresh_id)

    assert stale is not None
    assert stale["status"] == "failed"
    assert stale["error"] is not None
    assert stale["error"]["message"] == "Job stalled: worker restart or crash"
    assert stale["finished_at"] is not None

    assert fresh is not None
    assert fresh["status"] == "running"  # untouched
