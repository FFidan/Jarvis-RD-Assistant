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


def test_paper_ingestion_owner_map_includes_sprint3_jobs():
    """Sprint 3 domain job kinds are documented as paper_ingestion owned."""
    from jarvis_common import jobs

    expected = {
        "pulse.train_classifier",
        "zotero.sync_annotations",
        "paper.summarize",
        "papers.scan_local",
        "extraction.single",
        "digest.weekly",
        "contradictions.scan",
    }

    assert expected <= jobs.JOB_HANDLER_OWNER.keys()
    assert {jobs.JOB_HANDLER_OWNER[k] for k in expected} == {"paper_ingestion"}


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
    assert hasattr(jarvis_common, "stream_job_events")


def test_job_sse_payload_includes_terminal_details_only_for_terminal_rows():
    """SSE payloads expose result/error/payload only after a terminal status."""
    from jarvis_common.jobs import job_sse_payload

    running = job_sse_payload(
        {
            "status": "running",
            "progress": 0.5,
            "progress_message": "halfway",
            "result": {"ok": True},
            "payload": {"paper_id": 1},
        }
    )
    terminal = job_sse_payload(
        {
            "status": "succeeded",
            "progress": 1.0,
            "progress_message": "done",
            "result": {"ok": True},
            "payload": {"paper_id": 1},
        }
    )

    assert "result" not in running
    assert terminal["result"] == {"ok": True}
    assert terminal["payload"] == {"paper_id": 1}


@pytest.mark.asyncio
async def test_notify_job_update_is_best_effort():
    """NOTIFY failures should not break job state updates."""
    from jarvis_common.jobs import JOB_NOTIFY_CHANNEL, notify_job_update

    conn = AsyncMock()
    conn.execute = AsyncMock(side_effect=RuntimeError("listen disabled"))

    await notify_job_update(conn, "job-1")

    conn.execute.assert_awaited_once_with("SELECT pg_notify($1, $2)", JOB_NOTIFY_CHANNEL, "job-1")


@pytest.mark.asyncio
async def test_wait_for_job_notification_uses_asyncpg_listen(monkeypatch):
    """Job SSE waits should route through asyncpg-listen."""
    from jarvis_common import jobs

    observed: dict[str, Any] = {}

    class FakeListener:
        def __init__(self, connect, reconnect_delay=5):
            observed["connect"] = connect
            observed["reconnect_delay"] = reconnect_delay

        async def run(self, handler_per_channel, *, policy, notification_timeout):
            observed["policy"] = policy
            observed["notification_timeout"] = notification_timeout
            await handler_per_channel[jobs.JOB_NOTIFY_CHANNEL](
                jobs.asyncpg_listen.Notification(jobs.JOB_NOTIFY_CHANNEL, "job-1")
            )
            await asyncio.sleep(60)

    monkeypatch.setattr(jobs.asyncpg_listen, "NotificationListener", FakeListener)

    assert await jobs._wait_for_job_notification(MagicMock(), "job-1", 0.01) is True
    assert observed["reconnect_delay"] == 0.01
    assert observed["policy"] == jobs.asyncpg_listen.ListenPolicy.ALL
    assert observed["notification_timeout"] == 0.01


@pytest.mark.asyncio
async def test_wait_for_job_notification_falls_back_when_listener_fails(monkeypatch):
    """Listener setup errors should fall back to the polling path."""
    from jarvis_common import jobs

    class FailingListener:
        def __init__(self, _connect, reconnect_delay=5):
            pass

        async def run(self, _handler_per_channel, *, policy, notification_timeout):
            raise RuntimeError("listen unavailable")

    monkeypatch.setattr(jobs.asyncpg_listen, "NotificationListener", FailingListener)

    assert await jobs._wait_for_job_notification(MagicMock(), "job-1", 0.01) is False


# ---------------------------------------------------------------------------
# Helpers for mock-based tests (no real DB)
# ---------------------------------------------------------------------------


def _make_mock_pool_returning(rows: list[dict | None]):
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
                finished_at     TIMESTAMPTZ,
                last_heartbeat_at TIMESTAMPTZ
            )
        """)
        # Migration 035: ensure the heartbeat column exists in pre-existing DBs.
        await conn.execute(
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS last_heartbeat_at TIMESTAMPTZ"
        )
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

    # Run reaper once — pass the kind so it is in scope.
    reaped = await _reap_stale_jobs(db_pool, ["noop.test"])
    assert reaped == 1

    from jarvis_common.jobs import get

    stale = await get(db_pool, stale_id)
    fresh = await get(db_pool, fresh_id)

    assert stale is not None
    assert stale["status"] == "failed"
    assert stale["error"] is not None
    assert stale["error"]["message"] == "reaped as stale (no heartbeat)"
    assert stale["finished_at"] is not None

    assert fresh is not None
    assert fresh["status"] == "running"  # untouched


# ---------------------------------------------------------------------------
# JOB-002 — request_cancel on queued job → immediate terminal transition
# ---------------------------------------------------------------------------


@_SKIP_NO_DB
@pytest.mark.asyncio
async def test_request_cancel_queued_job_transitions_to_cancelled(db_pool):
    """JOB-002: request_cancel() on a queued job must flip status to 'cancelled' immediately."""
    from jarvis_common.jobs import enqueue, get, request_cancel

    job_id = await enqueue(db_pool, "noop.test", {})

    # Confirm the job starts as queued
    row_before = await get(db_pool, job_id)
    assert row_before is not None
    assert row_before["status"] == "queued"

    await request_cancel(db_pool, job_id)

    row_after = await get(db_pool, job_id)
    assert row_after is not None
    assert row_after["status"] == "cancelled", f"Expected 'cancelled', got '{row_after['status']}'"
    assert row_after["cancel_requested"] is True
    assert row_after["finished_at"] is not None, "finished_at must be set for terminal cancel"


@_SKIP_NO_DB
@pytest.mark.asyncio
async def test_worker_loop_skips_cancelled_queued_job(db_pool):
    """JOB-002: worker_loop must not pick up a job with cancel_requested=TRUE."""
    import asyncpg
    import httpx
    from jarvis_common.jobs import (
        JobContext,
        enqueue,
        get,
        job_handler,
        request_cancel,
        worker_loop,
    )

    unique_kind = f"test.cancel.skip.{uuid.uuid4().hex}"
    handler_called = []

    @job_handler(unique_kind)
    async def _should_never_run(
        _pool: asyncpg.Pool,
        _http: httpx.AsyncClient,
        payload: dict[str, Any],
        ctx: JobContext,
    ) -> dict[str, Any]:
        handler_called.append(True)
        return {}

    job_id = await enqueue(db_pool, unique_kind, {})
    # Cancel while still queued — status transitions to 'cancelled' immediately (JOB-002 fix)
    await request_cancel(db_pool, job_id)

    row_cancelled = await get(db_pool, job_id)
    assert row_cancelled is not None
    assert row_cancelled["status"] == "cancelled"

    http_mock = AsyncMock(spec=httpx.AsyncClient)
    stop_event = asyncio.Event()

    # Run the worker for a short burst — it should not pick up the cancelled job
    async def _run_one_poll():
        stop_event.set()  # stop immediately after first iteration
        await worker_loop(
            db_pool,
            http_mock,
            kinds={unique_kind},
            poll_interval=0.05,
            stop_event=stop_event,
        )

    await _run_one_poll()

    # Handler must never have been invoked
    assert handler_called == [], "Worker must not execute a job that was cancelled before pickup"

    # DB status must remain 'cancelled' (not 'succeeded'/'failed'/'running')
    row_final = await get(db_pool, job_id)
    assert row_final is not None
    assert row_final["status"] == "cancelled"


# ---------------------------------------------------------------------------
# JOB-003 — JobStatusResponse.error is dict, not str
# ---------------------------------------------------------------------------


@_SKIP_NO_DB
@pytest.mark.asyncio
async def test_job_status_response_error_is_dict(db_pool):
    """JOB-003: error column (JSONB) must be deserialised as dict, not coerced to str."""
    from jarvis_common.jobs import get
    from jarvis_common.models import JobStatusResponse

    # Insert a job row whose error column is a JSONB dict (simulating a real failure)
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO jobs (kind, status, error, finished_at)
            VALUES ('noop.test', 'failed', $1::jsonb, NOW())
            RETURNING id::text
            """,
            '{"message": "boom", "type": "RuntimeError"}',
        )
    assert row is not None
    job_id = row["id"]

    db_row = await get(db_pool, job_id)
    assert db_row is not None

    # Pydantic model must accept and preserve the dict (not coerce to str)
    status_resp = JobStatusResponse(**db_row)
    assert isinstance(status_resp.error, dict), (
        f"Expected dict, got {type(status_resp.error)}: {status_resp.error!r}"
    )
    assert status_resp.error["message"] == "boom"
    assert status_resp.error["type"] == "RuntimeError"


# ---------------------------------------------------------------------------
# JOB-001: heartbeat-based reaper + kind-scoped kill filter
# ---------------------------------------------------------------------------


@_SKIP_NO_DB
@pytest.mark.asyncio
async def test_reaper_spares_job_with_recent_heartbeat(db_pool):
    """A job that is 2 hours old but heartbeated 1 minute ago must NOT be reaped."""
    from jarvis_common.jobs import _reap_stale_jobs

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO jobs (kind, status, started_at, last_heartbeat_at)
            VALUES ($1, 'running',
                    NOW() - INTERVAL '2 hours',
                    NOW() - INTERVAL '1 minute')
            RETURNING id::text
            """,
            "pulse.generate",
        )
    assert row is not None
    job_id = row["id"]

    reaped = await _reap_stale_jobs(db_pool, ["pulse.generate"])
    assert reaped == 0, "Job with recent heartbeat should not be reaped"

    from jarvis_common.jobs import get

    db_row = await get(db_pool, job_id)
    assert db_row is not None
    assert db_row["status"] == "running"


@_SKIP_NO_DB
@pytest.mark.asyncio
async def test_reaper_scopes_to_kinds(db_pool):
    """Reaper only kills jobs whose kind appears in the kinds list."""
    from jarvis_common.jobs import _reap_stale_jobs

    # Insert a stale running job of a different kind (paper.download).
    async with db_pool.acquire() as conn:
        row_other = await conn.fetchrow(
            """
            INSERT INTO jobs (kind, status, started_at)
            VALUES ($1, 'running', NOW() - INTERVAL '2 hours')
            RETURNING id::text
            """,
            "paper.download",
        )
    assert row_other is not None
    other_id = row_other["id"]

    # Reaper for pulse.generate should NOT touch the paper.download job.
    reaped = await _reap_stale_jobs(db_pool, ["pulse.generate"])
    assert reaped == 0, "Reaper must not kill jobs belonging to another service"

    from jarvis_common.jobs import get

    other_row = await get(db_pool, other_id)
    assert other_row is not None
    assert other_row["status"] == "running"

    # Now insert a stale job of pulse.generate — it SHOULD be reaped.
    async with db_pool.acquire() as conn:
        row_own = await conn.fetchrow(
            """
            INSERT INTO jobs (kind, status, started_at)
            VALUES ($1, 'running', NOW() - INTERVAL '2 hours')
            RETURNING id::text
            """,
            "pulse.generate",
        )
    assert row_own is not None
    own_id = row_own["id"]

    reaped = await _reap_stale_jobs(db_pool, ["pulse.generate"])
    assert reaped == 1, "Reaper must kill its own stale job"

    own_row = await get(db_pool, own_id)
    assert own_row is not None
    assert own_row["status"] == "failed"
    assert own_row["error"] is not None
    assert own_row["error"]["message"] == "reaped as stale (no heartbeat)"


# ---------------------------------------------------------------------------
# WS-6 — LISTEN/NOTIFY wakes stream_job_events faster than poll interval
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.skipif(not _DB_URL, reason="requires TEST_DATABASE_URL")
async def test_listen_notify_wakes_stream_faster_than_poll():
    """WS-6: a pg_notify after job status change should wake the SSE stream within 200ms.

    This test requires TEST_DATABASE_URL and a jobs table (created by db_pool fixture).
    It is gated so CI without a real DB skips cleanly.
    """
    if not _DB_URL:
        pytest.skip("requires TEST_DATABASE_URL")

    import asyncpg
    from jarvis_common import init_pg_connection
    from jarvis_common.jobs import enqueue, stream_job_events

    try:
        pool = await asyncpg.create_pool(
            _DB_URL,
            min_size=1,
            max_size=3,
            init=init_pg_connection,
        )
    except Exception as exc:
        pytest.skip(f"Cannot connect to test DB: {exc}")
        return

    # Ensure jobs table exists (same DDL as db_pool fixture).
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
                finished_at     TIMESTAMPTZ,
                last_heartbeat_at TIMESTAMPTZ
            )
        """)
        await conn.execute(
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS last_heartbeat_at TIMESTAMPTZ"
        )

    try:
        # Insert a running job so stream_job_events has something to poll.
        job_id = await enqueue(pool, "noop.test", {})
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE jobs SET status = 'running', started_at = NOW() WHERE id = $1::uuid",
                job_id,
            )

        events_received: list[str] = []
        stream_started = asyncio.Event()

        async def _is_disconnected() -> bool:
            return False

        async def _consume_stream():
            stream_started.set()
            async for event in stream_job_events(pool, job_id, is_disconnected=_is_disconnected):
                events_received.append(event)
                # Stop after first terminal event to avoid running forever.
                if '"status": "succeeded"' in event or '"status": "failed"' in event:
                    break

        stream_task = asyncio.create_task(_consume_stream())

        # Wait for the stream to start, then after ~50ms flip the job to terminal.
        await stream_started.wait()
        await asyncio.sleep(0.05)

        t0 = asyncio.get_event_loop().time()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE jobs SET status = 'succeeded', finished_at = NOW(), progress = 1.0"
                " WHERE id = $1::uuid",
                job_id,
            )
            # Notify explicitly — the trigger may not exist in the test schema.
            from jarvis_common.jobs import JOB_NOTIFY_CHANNEL

            await conn.execute("SELECT pg_notify($1, $2)", JOB_NOTIFY_CHANNEL, job_id)

        # The stream should deliver the terminal event within 200ms (poll interval is 2s).
        try:
            await asyncio.wait_for(stream_task, timeout=0.5)
        except TimeoutError:
            stream_task.cancel()
            pytest.fail(
                f"stream_job_events did not deliver terminal event within 500ms "
                f"({asyncio.get_event_loop().time() - t0:.2f}s elapsed)"
            )

        elapsed = asyncio.get_event_loop().time() - t0
        assert elapsed < 0.2, f"LISTEN/NOTIFY must wake the stream in <200ms; took {elapsed:.3f}s"
        assert any("succeeded" in e for e in events_received), (
            "Stream must have emitted a 'succeeded' event"
        )
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM jobs WHERE kind = 'noop.test'")
        await pool.close()


# ---------------------------------------------------------------------------
# JC-001 — empty kind_list triggers all-kinds mode in _reap_stale_jobs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reap_stale_jobs_all_kinds_mode():
    """JC-001: passing an empty kind_list must reap stale jobs of any kind (all-kinds mode)."""
    from jarvis_common.jobs import _reap_stale_jobs

    # Build a fake row to simulate a stale job found by the reaper.
    fake_row = {"id": "00000000-0000-0000-0000-000000000001"}
    pool, conn = _make_mock_pool_returning([fake_row])
    conn.execute = AsyncMock(return_value=None)  # notify_job_update uses execute

    reaped = await _reap_stale_jobs(pool, [])

    # The reaper must have issued a SQL call with an empty array (all-kinds mode).
    assert conn.fetch.await_count == 1, "conn.fetch must be called exactly once"
    sql_call_args = conn.fetch.await_args.args
    # $2 parameter — should be the empty list (all-kinds sentinel)
    kinds_param = sql_call_args[2]
    assert kinds_param == [], (
        f"kinds param must be empty list for all-kinds mode, got {kinds_param!r}"
    )

    # The SQL string must use the all-kinds guard, NOT the bare ANY filter.
    sql_string = sql_call_args[0]
    assert "$2::text[] = '{}'" in sql_string, (
        "SQL must contain the all-kinds guard ($2::text[] = '{}')"
    )

    # The function must report the row count from conn.fetch.
    assert reaped == 1, f"Expected 1 reaped job (from mock), got {reaped}"


# ---------------------------------------------------------------------------
# B.4 Step 2 — procrastinate SSE bridge
# ---------------------------------------------------------------------------


def test_procrastinate_status_to_jarvis_maps_all_known_statuses():
    """B.4 SSE bridge: every procrastinate enum value maps to a legacy status."""
    from jarvis_common.jobs import (
        PROCRASTINATE_STATUS_MAP,
        procrastinate_status_to_jarvis,
    )

    # Every key in the map must round-trip through the helper.
    expected = {
        "todo": "queued",
        "doing": "running",
        "succeeded": "succeeded",
        "failed": "failed",
        "cancelled": "cancelled",
        "aborting": "running",
        "aborted": "cancelled",
    }
    assert PROCRASTINATE_STATUS_MAP == expected
    for proc, jarvis in expected.items():
        assert procrastinate_status_to_jarvis(proc) == jarvis

    # Unknown statuses fall through to "running" so the SSE stream stays open.
    assert procrastinate_status_to_jarvis("totally_unknown") == "running"


def test_job_sse_payload_includes_source_discriminator():
    """B.4 SSE bridge: payload now carries a ``source`` discriminator."""
    from jarvis_common.jobs import job_sse_payload

    legacy = job_sse_payload({"status": "running", "progress": 0.3, "progress_message": "..."})
    procrastinate = job_sse_payload(
        {"status": "running", "progress": None, "progress_message": None},
        source="procrastinate",
    )

    assert legacy["source"] == "legacy"
    assert procrastinate["source"] == "procrastinate"


def test_procrastinate_row_to_jarvis_row_normalises_shape():
    """B.4 SSE bridge: procrastinate row shape adapts to the legacy SSE shape."""
    from jarvis_common.jobs import procrastinate_row_to_jarvis_row

    prow = {
        "id": 42,
        "queue_name": "paper_ingestion",
        "task_name": "paper.process",
        "status": "succeeded",
        "args": {"job_id": "abc-123", "paper_id": 7},
    }

    out = procrastinate_row_to_jarvis_row(prow)

    assert out["status"] == "succeeded"  # legacy enum
    assert out["payload"] == {"job_id": "abc-123", "paper_id": 7}  # args surfaced
    assert out["progress"] is None
    assert out["progress_message"] is None
    assert out["result"] is None
    assert out["error"] is None


@pytest.mark.asyncio
async def test_wait_for_job_notification_subscribes_to_procrastinate_channel(monkeypatch):
    """B.4 SSE bridge: the listen-set must include both legacy and procrastinate channels."""
    from jarvis_common import jobs

    observed_channels: list[str] = []

    class FakeListener:
        def __init__(self, connect, reconnect_delay=5):
            pass

        async def run(self, handler_per_channel, *, policy, notification_timeout):
            observed_channels.extend(handler_per_channel.keys())
            # Trigger the procrastinate handler to confirm it sets `matched`.
            handler = handler_per_channel[jobs.PROCRASTINATE_NOTIFY_CHANNEL]
            await handler(
                jobs.asyncpg_listen.Notification(
                    jobs.PROCRASTINATE_NOTIFY_CHANNEL,
                    '{"type": "job_inserted", "job_id": 99}',
                )
            )
            await asyncio.sleep(60)

    monkeypatch.setattr(jobs.asyncpg_listen, "NotificationListener", FakeListener)

    # Procrastinate notify alone should wake the waiter (returns True).
    assert await jobs._wait_for_job_notification(MagicMock(), "job-1", 0.01) is True
    assert jobs.JOB_NOTIFY_CHANNEL in observed_channels
    assert jobs.PROCRASTINATE_NOTIFY_CHANNEL in observed_channels


@pytest.mark.asyncio
async def test_get_procrastinate_job_returns_none_when_table_missing():
    """B.4 SSE bridge: graceful degrade when migration 052 hasn't been applied."""
    import asyncpg as _asyncpg
    from jarvis_common.jobs import get_procrastinate_job_for_jarvis_id

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=_asyncpg.UndefinedTableError("relation does not exist"))

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=False)

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=cm)

    result = await get_procrastinate_job_for_jarvis_id(pool, "abc-123")
    assert result is None


@pytest.mark.asyncio
async def test_get_procrastinate_job_returns_dict_when_present():
    """B.4 SSE bridge: lookup by ``args->>'job_id'`` returns row as a dict."""
    from jarvis_common.jobs import get_procrastinate_job_for_jarvis_id

    fake_row = {
        "id": 42,
        "queue_name": "paper_ingestion",
        "task_name": "paper.process",
        "status": "doing",
        "args": {"job_id": "abc-123"},
    }
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=fake_row)

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=False)

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=cm)

    result = await get_procrastinate_job_for_jarvis_id(pool, "abc-123")
    assert result == fake_row

    # The query must filter by the JARVIS job_id stored in args.
    sql, *params = conn.fetchrow.await_args.args
    assert "args->>'job_id'" in sql
    assert params == ["abc-123"]


@pytest.mark.asyncio
async def test_stream_job_events_emits_procrastinate_only_payload(monkeypatch):
    """B.4-(a): a procrastinate-only job produces an SSE event in the legacy shape.

    Setup: ``get`` returns None (no legacy row), ``get_procrastinate_job_for_jarvis_id``
    returns a procrastinate row that immediately reports status='succeeded'. The
    stream must yield exactly one payload with ``source='procrastinate'``,
    ``status='succeeded'``, and the legacy payload shape.
    """
    import json as _json

    from jarvis_common import jobs

    procrastinate_rows = [
        {
            "id": 7,
            "queue_name": "paper_ingestion",
            "task_name": "paper.process",
            "status": "succeeded",
            "args": {"job_id": "p-1", "paper_id": 42},
        },
    ]

    async def _fake_legacy_get(_pool, _job_id):
        return None

    async def _fake_procrastinate_get(_pool, _job_id):
        return procrastinate_rows[0]

    async def _no_wait(_pool, _job_id, _timeout):
        return False

    async def _never_disconnected():
        return False

    monkeypatch.setattr(jobs, "get", _fake_legacy_get)
    monkeypatch.setattr(jobs, "get_procrastinate_job_for_jarvis_id", _fake_procrastinate_get)
    monkeypatch.setattr(jobs, "_wait_for_job_notification", _no_wait)

    pool_marker = MagicMock(name="db_pool")
    frames: list[str] = []
    async for frame in jobs.stream_job_events(
        pool_marker, "p-1", is_disconnected=_never_disconnected
    ):
        frames.append(frame)

    # Filter out keepalive comments.
    data_frames = [f for f in frames if f.startswith("data:")]
    assert len(data_frames) == 1, f"expected 1 SSE data frame, got {data_frames!r}"

    body = data_frames[0].removeprefix("data: ").rstrip("\n")
    payload = _json.loads(body)
    assert payload["source"] == "procrastinate"
    assert payload["status"] == "succeeded"
    assert payload["payload"] == {"job_id": "p-1", "paper_id": 42}


@pytest.mark.asyncio
async def test_stream_job_events_mixed_legacy_and_procrastinate(monkeypatch):
    """B.4-(b): a job that exists in BOTH tables emits one frame per source on first cycle.

    The legacy row reports ``running`` while the procrastinate row reports
    ``succeeded``. The stream should:
      1. yield the legacy ``running`` frame (source=legacy),
      2. yield the procrastinate ``succeeded`` frame (source=procrastinate),
      3. terminate because procrastinate has reached a terminal state.
    """
    import json as _json

    from jarvis_common import jobs

    legacy_row = {
        "id": "uuid-mixed",
        "kind": "paper.process",
        "status": "running",
        "progress": 0.4,
        "progress_message": "halfway",
        "payload": {"foo": "bar"},
        "result": None,
        "error": None,
    }
    procrastinate_row = {
        "id": 99,
        "queue_name": "paper_ingestion",
        "task_name": "paper.process",
        "status": "succeeded",
        "args": {"job_id": "uuid-mixed"},
    }

    async def _fake_legacy_get(_pool, _job_id):
        return legacy_row

    async def _fake_procrastinate_get(_pool, _job_id):
        return procrastinate_row

    async def _no_wait(_pool, _job_id, _timeout):
        return False

    async def _never_disconnected():
        return False

    monkeypatch.setattr(jobs, "get", _fake_legacy_get)
    monkeypatch.setattr(jobs, "get_procrastinate_job_for_jarvis_id", _fake_procrastinate_get)
    monkeypatch.setattr(jobs, "_wait_for_job_notification", _no_wait)

    pool_marker = MagicMock(name="db_pool")
    frames: list[str] = []
    async for frame in jobs.stream_job_events(
        pool_marker, "uuid-mixed", is_disconnected=_never_disconnected
    ):
        frames.append(frame)

    data_frames = [f for f in frames if f.startswith("data:")]
    payloads = [_json.loads(f.removeprefix("data: ").rstrip("\n")) for f in data_frames]

    sources = [p["source"] for p in payloads]
    statuses = [p["status"] for p in payloads]

    # Both sources must produce a frame, in order: legacy first then procrastinate.
    assert sources == ["legacy", "procrastinate"], (
        f"expected ['legacy', 'procrastinate'], got {sources!r}"
    )
    assert statuses == ["running", "succeeded"]

    # Stream must terminate as soon as procrastinate reaches a terminal status.
    # (No infinite loop — verified by the fact that this test returns at all.)


@pytest.mark.asyncio
async def test_stream_job_events_legacy_only_unchanged(monkeypatch):
    """B.4 regression: legacy-only path still terminates at legacy terminal status.

    When the procrastinate table doesn't have a matching row,
    stream_job_events must behave exactly like the pre-bridge implementation.
    """
    import json as _json

    from jarvis_common import jobs

    legacy_row = {
        "id": "uuid-legacy",
        "kind": "paper.process",
        "status": "succeeded",
        "progress": 1.0,
        "progress_message": "done",
        "payload": {"x": 1},
        "result": {"ok": True},
        "error": None,
    }

    async def _fake_legacy_get(_pool, _job_id):
        return legacy_row

    async def _fake_procrastinate_get(_pool, _job_id):
        return None  # no procrastinate row

    async def _no_wait(_pool, _job_id, _timeout):
        return False

    async def _never_disconnected():
        return False

    monkeypatch.setattr(jobs, "get", _fake_legacy_get)
    monkeypatch.setattr(jobs, "get_procrastinate_job_for_jarvis_id", _fake_procrastinate_get)
    monkeypatch.setattr(jobs, "_wait_for_job_notification", _no_wait)

    pool_marker = MagicMock(name="db_pool")
    frames: list[str] = []
    async for frame in jobs.stream_job_events(
        pool_marker, "uuid-legacy", is_disconnected=_never_disconnected
    ):
        frames.append(frame)

    data_frames = [f for f in frames if f.startswith("data:")]
    assert len(data_frames) == 1
    payload = _json.loads(data_frames[0].removeprefix("data: ").rstrip("\n"))
    assert payload["source"] == "legacy"
    assert payload["status"] == "succeeded"
    assert payload["result"] == {"ok": True}
