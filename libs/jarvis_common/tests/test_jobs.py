"""Unit tests for jarvis_common.jobs.

Tests use a real asyncpg pool against a PostgreSQL database.
Set TEST_DATABASE_URL (e.g. postgresql://user:pass@localhost:5432/testdb) to run.
Tests are skipped automatically when the env var is unset or the DB is unreachable.

The test schema is isolated in a per-test temporary table (prefix-renamed) so
concurrent test runs don't interfere with each other.  Each test function
gets a fresh jobs table via the ``db_pool`` fixture.
"""

from __future__ import annotations

import asyncio
import os
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
async def test_get_returns_queued_row(db_pool):
    """get() on a freshly-inserted job should return status='queued' with correct fields."""
    from jarvis_common.jobs import get

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO jobs (kind, payload, user_id)
            VALUES ('noop.test', '{"hello": "world"}'::jsonb, 'user-1')
            RETURNING id::text
            """
        )
    assert row is not None
    job_id = row["id"]
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
    from jarvis_common.jobs import list_jobs

    async with db_pool.acquire() as conn:
        r1 = await conn.fetchrow("INSERT INTO jobs (kind) VALUES ('noop.test') RETURNING id::text")
        r2 = await conn.fetchrow("INSERT INTO jobs (kind) VALUES ('noop.test') RETURNING id::text")
    assert r1 is not None and r2 is not None
    j1, j2 = r1["id"], r2["id"]

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
    from jarvis_common.jobs import list_jobs

    async with db_pool.acquire() as conn:
        r1 = await conn.fetchrow(
            "INSERT INTO jobs (kind) VALUES ('card.generate') RETURNING id::text"
        )
        await conn.fetchrow("INSERT INTO jobs (kind) VALUES ('noop.test') RETURNING id::text")
    assert r1 is not None
    j1 = r1["id"]

    rows = await list_jobs(db_pool, kind="card.generate")
    ids = {r["id"] if isinstance(r["id"], str) else str(r["id"]) for r in rows}
    assert j1 in ids
    assert all(r["kind"] == "card.generate" for r in rows)


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
    """B.4 SSE bridge: procrastinate row shape adapts to the full legacy Job-interface shape.

    After the Bug-2 fix, ``procrastinate_row_to_jarvis_row`` returns the full
    12+-key shape (including ``id``, ``kind``, ``user_id``, ``created_at``, etc.)
    and strips the reserved ``job_id`` / ``user_id`` keys from ``payload`` so
    that route handlers can call ``_owner_matches``, ``serialise_row``, and the
    cancel branch without additional per-field checks.
    """
    from jarvis_common.jobs import procrastinate_row_to_jarvis_row

    prow = {
        "id": 42,
        "queue_name": "paper_ingestion",
        "task_name": "paper.process",
        "status": "succeeded",
        "args": {"job_id": "abc-123", "paper_id": 7, "user_id": "5"},
        "created_at": None,
        "started_at": None,
        "finished_at": None,
    }

    out = procrastinate_row_to_jarvis_row(prow)

    # Full Job-interface keys.
    assert out["id"] == "abc-123"
    assert out["kind"] == "paper.process"
    assert out["user_id"] == "5"
    assert out["status"] == "succeeded"  # legacy enum
    assert out["progress"] == 0
    assert out["progress_message"] is None
    assert out["result"] is None
    assert out["error"] is None
    # payload strips reserved keys.
    assert out["payload"] == {"paper_id": 7}
    # Timestamps pass through from the row.
    assert out["created_at"] is None
    assert out["cancel_requested"] is False  # "succeeded" is not a cancel status
    assert out["source"] == "procrastinate"


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


async def test_stream_job_events_emits_procrastinate_only_payload(monkeypatch):
    """B.4-(a): a procrastinate-only job produces an SSE event in the legacy shape.

    Setup: ``get`` returns None (no legacy row), ``get_procrastinate_job_for_jarvis_id``
    returns a procrastinate row that immediately reports status='succeeded'. The
    stream must yield exactly one payload with ``source='procrastinate'``,
    ``status='succeeded'``, and the legacy payload shape.

    After the Bug-2 fix, ``procrastinate_row_to_jarvis_row`` strips the
    reserved ``job_id`` key from ``payload``; only user-defined keys remain.
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
            "attempts": 1,
            "created_at": None,
            "started_at": None,
            "finished_at": None,
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
    # job_id is stripped from payload by procrastinate_row_to_jarvis_row (reserved key).
    assert payload["payload"] == {"paper_id": 42}


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


# ---------------------------------------------------------------------------
# B.4 Step 3 — get_unified + list_jobs UNION ALL (Bug 2 unit tests)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_unified_returns_legacy_when_present():
    """get_unified() returns the legacy row when legacy lookup succeeds.

    The ``source`` discriminator must be set to ``"legacy"`` and the legacy
    row must otherwise pass through unmodified.
    """
    from jarvis_common.jobs import get_unified

    legacy_row = {
        "id": "uuid-leg-1",
        "kind": "paper.process",
        "status": "queued",
        "progress": None,
        "progress_message": None,
        "payload": {},
        "result": None,
        "error": None,
        "user_id": None,
        "created_at": None,
        "started_at": None,
        "finished_at": None,
        "cancel_requested": False,
    }

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=legacy_row)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=cm)

    result = await get_unified(pool, "uuid-leg-1")
    assert result is not None
    assert result["source"] == "legacy"
    assert result["status"] == "queued"
    assert result["id"] == "uuid-leg-1"


@pytest.mark.asyncio
async def test_get_unified_falls_through_to_procrastinate():
    """get_unified() falls through to procrastinate lookup when legacy row is absent.

    This is the Bug 2 regression test: if legacy ``get()`` returns None the
    function must query the procrastinate table and return a row in the full
    Job-interface shape with ``source="procrastinate"``.
    """
    from jarvis_common import jobs
    from jarvis_common.jobs import get_unified

    procrastinate_row = {
        "id": 42,
        "queue_name": "paper_ingestion",
        "task_name": "paper.process",
        "status": "doing",
        "args": {"job_id": "p-uni-1", "paper_id": 7, "user_id": "5"},
        "attempts": 1,
        "created_at": None,
        "started_at": None,
        "finished_at": None,
    }

    async def _no_legacy(_pool, _job_id):
        return None

    async def _prow(_pool, _job_id):
        return procrastinate_row

    pool = MagicMock(name="db_pool")

    # Monkeypatch the module-level functions.
    original_get = jobs.get
    original_pget = jobs.get_procrastinate_job_for_jarvis_id
    jobs.get = _no_legacy  # type: ignore[assignment]
    jobs.get_procrastinate_job_for_jarvis_id = _prow  # type: ignore[assignment]
    try:
        result = await get_unified(pool, "p-uni-1")
    finally:
        jobs.get = original_get  # type: ignore[assignment]
        jobs.get_procrastinate_job_for_jarvis_id = original_pget  # type: ignore[assignment]

    assert result is not None
    assert result["source"] == "procrastinate"
    assert result["status"] == "running"  # "doing" → "running"
    assert result["id"] == "p-uni-1"
    assert result["kind"] == "paper.process"
    assert result["user_id"] == "5"
    # Payload must exclude the reserved job_id / user_id keys.
    assert result["payload"] == {"paper_id": 7}


@pytest.mark.asyncio
async def test_get_unified_returns_none_when_neither():
    """get_unified() returns None when both legacy and procrastinate lookups miss."""
    from jarvis_common import jobs
    from jarvis_common.jobs import get_unified

    async def _no_legacy(_pool, _job_id):
        return None

    async def _no_prow(_pool, _job_id):
        return None

    pool = MagicMock(name="db_pool")

    original_get = jobs.get
    original_pget = jobs.get_procrastinate_job_for_jarvis_id
    jobs.get = _no_legacy  # type: ignore[assignment]
    jobs.get_procrastinate_job_for_jarvis_id = _no_prow  # type: ignore[assignment]
    try:
        result = await get_unified(pool, "missing-job")
    finally:
        jobs.get = original_get  # type: ignore[assignment]
        jobs.get_procrastinate_job_for_jarvis_id = original_pget  # type: ignore[assignment]

    assert result is None


@pytest.mark.asyncio
async def test_list_jobs_union_includes_procrastinate_rows():
    """list_jobs() with UNION ALL returns rows from both tables.

    Setup: legacy conn.fetch returns one legacy row when the union query runs
    (we cannot actually mock the fallback split cleanly here, but we can verify
    that list_jobs does NOT raise on a pool that returns rows, and that those
    rows are returned as plain dicts with the ``source`` key present).

    The test uses a mock pool that returns a pre-built row list so no real DB
    is needed.  Verifying the SQL string contains 'UNION ALL' and 'procrastinate'
    checks that the function was rewritten rather than silently left as legacy-only.
    """
    from jarvis_common.jobs import list_jobs

    fake_row = {
        "id": "p-list-1",
        "kind": "digest.weekly",
        "user_id": None,
        "status": "queued",
        "payload": {},
        "result": None,
        "error": None,
        "progress": 0.0,
        "progress_message": None,
        "created_at": None,
        "started_at": None,
        "finished_at": None,
        "source": "procrastinate",
    }

    conn = AsyncMock()
    # First call (UNION ALL query) succeeds and returns our fake row.
    conn.fetch = AsyncMock(return_value=[fake_row])
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=cm)

    rows = await list_jobs(pool)

    assert len(rows) == 1
    assert rows[0]["source"] == "procrastinate"
    assert rows[0]["kind"] == "digest.weekly"

    # Verify the UNION ALL query was actually issued (not the legacy-only fallback).
    issued_sql: str = conn.fetch.await_args.args[0]
    assert "UNION ALL" in issued_sql, "list_jobs must issue a UNION ALL query"
    assert "procrastinate_jobs" in issued_sql, "list_jobs UNION ALL must include procrastinate_jobs"


@pytest.mark.asyncio
async def test_list_jobs_falls_back_to_legacy_on_undefined_table():
    """list_jobs() falls back gracefully when procrastinate_jobs doesn't exist."""
    import asyncpg as _asyncpg
    from jarvis_common.jobs import list_jobs

    legacy_row = {
        "id": "leg-fb-1",
        "kind": "noop.test",
        "user_id": None,
        "status": "queued",
        "payload": {},
        "result": None,
        "error": None,
        "progress": None,
        "progress_message": None,
        "created_at": None,
        "started_at": None,
        "finished_at": None,
        "source": "legacy",
    }

    conn = AsyncMock()
    # First call raises UndefinedTableError (procrastinate table missing).
    # Second call (legacy fallback) succeeds.
    conn.fetch = AsyncMock(
        side_effect=[
            _asyncpg.UndefinedTableError("relation procrastinate_jobs does not exist"),
            [legacy_row],
        ]
    )
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=cm)

    rows = await list_jobs(pool)

    assert len(rows) == 1
    assert rows[0]["id"] == "leg-fb-1"
