"""Unit tests for jarvis_common.jobs."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

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
            "error": {"message": "boom"},
            "payload": {"paper_id": 1},
        }
    )

    assert "result" not in running
    assert terminal["result"] == {"ok": True}
    assert terminal["error"] == {"message": "boom"}
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
        "aborting": "cancelled",
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
        "result": {"cards_created": 3},
        "error": None,
    }

    out = procrastinate_row_to_jarvis_row(prow)

    # Full Job-interface keys.
    assert out["id"] == "abc-123"
    assert out["kind"] == "paper.process"
    assert out["user_id"] == "5"
    assert out["status"] == "succeeded"  # legacy enum
    assert out["progress"] == 0
    assert out["progress_message"] is None
    assert out["result"] == {"cards_created": 3}
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
    """A procrastinate job produces an SSE event in the legacy shape.

    ``get_procrastinate_job_for_jarvis_id`` returns a row that immediately
    reports status='succeeded'. The stream must yield exactly one payload with
    ``source='procrastinate'``, ``status='succeeded'``, and the legacy payload
    shape.  ``procrastinate_row_to_jarvis_row`` strips the reserved ``job_id``
    key from ``payload``; only user-defined keys remain.
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

    async def _fake_procrastinate_get(_pool, _job_id):
        return procrastinate_rows[0]

    async def _no_wait(_pool, _job_id, _timeout):
        return False

    async def _never_disconnected():
        return False

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


# ---------------------------------------------------------------------------
# B.4 Step 3 — get_unified + list_jobs (Bug 2 unit tests)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_unified_returns_procrastinate_when_present():
    """get_unified() returns the procrastinate row in the Job-interface shape.

    The function must query the procrastinate table and return a row with
    ``source="procrastinate"`` in the full Job-interface shape.
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

    async def _prow(_pool, _job_id):
        return procrastinate_row

    pool = MagicMock(name="db_pool")

    original_pget = jobs.get_procrastinate_job_for_jarvis_id
    jobs.get_procrastinate_job_for_jarvis_id = _prow  # type: ignore[assignment]
    try:
        result = await get_unified(pool, "p-uni-1")
    finally:
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
    """get_unified() returns None when the procrastinate lookup misses."""
    from jarvis_common import jobs
    from jarvis_common.jobs import get_unified

    async def _no_prow(_pool, _job_id):
        return None

    pool = MagicMock(name="db_pool")

    original_pget = jobs.get_procrastinate_job_for_jarvis_id
    jobs.get_procrastinate_job_for_jarvis_id = _no_prow  # type: ignore[assignment]
    try:
        result = await get_unified(pool, "missing-job")
    finally:
        jobs.get_procrastinate_job_for_jarvis_id = original_pget  # type: ignore[assignment]

    assert result is None


@pytest.mark.asyncio
async def test_list_jobs_returns_only_procrastinate_rows():
    """list_jobs() queries procrastinate_jobs exclusively and returns plain dicts.

    Verifies that the SQL issued queries procrastinate_jobs and that rows are
    returned with ``source='procrastinate'``.
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

    # Verify the query targets procrastinate_jobs.
    issued_sql: str = conn.fetch.await_args.args[0]
    assert "procrastinate_jobs" in issued_sql, "list_jobs must query procrastinate_jobs"


@pytest.mark.asyncio
async def test_list_jobs_fallback_omits_job_progress_alias_when_missing():
    """list_jobs() fallback must not reference jp after removing the progress join."""
    import asyncpg as _asyncpg
    from jarvis_common.jobs import list_jobs

    fallback_row = {
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
    conn.fetch = AsyncMock(
        side_effect=[
            _asyncpg.UndefinedTableError('relation "job_progress" does not exist'),
            [fallback_row],
        ]
    )
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=cm)

    rows = await list_jobs(pool, status="queued", kind="digest.weekly", user_id="u1", limit=5)

    assert rows == [fallback_row]
    assert conn.fetch.await_count == 2
    fallback_sql = conn.fetch.await_args_list[1].args[0]
    assert "LEFT JOIN job_progress" not in fallback_sql
    assert "jp." not in fallback_sql
    assert "NULL::jsonb AS result" in fallback_sql
    assert "NULL::jsonb AS error" in fallback_sql
    assert conn.fetch.await_args_list[1].args[1:] == ("queued", "digest.weekly", "u1", 5)


# ---------------------------------------------------------------------------
# job_progress LEFT JOIN — migration 054 wiring
# ---------------------------------------------------------------------------


def test_procrastinate_row_to_jarvis_row_lifts_progress_from_join():
    """When the LEFT JOIN populates progress/outcomes, they propagate."""
    from jarvis_common.jobs import procrastinate_row_to_jarvis_row

    prow = {
        "id": 42,
        "queue_name": "paper_ingestion",
        "task_name": "paper.process",
        "status": "doing",
        "args": {"job_id": "abc-123"},
        "progress": 0.75,
        "progress_message": "almost there",
        "result": {"done": True},
        "error": {"message": "ignored"},
        "created_at": None,
        "started_at": None,
        "finished_at": None,
    }

    out = procrastinate_row_to_jarvis_row(prow)

    assert out["progress"] == 0.75
    assert out["progress_message"] == "almost there"
    assert out["result"] == {"done": True}
    assert out["error"] == {"message": "ignored"}


def test_procrastinate_row_to_jarvis_row_defaults_when_join_missed():
    """Missing job_progress row → progress=0, progress_message=None (legacy contract)."""
    from jarvis_common.jobs import procrastinate_row_to_jarvis_row

    prow = {
        "id": 42,
        "queue_name": "paper_ingestion",
        "task_name": "paper.process",
        "status": "doing",
        "args": {"job_id": "abc-123"},
        "progress": None,
        "progress_message": None,
    }

    out = procrastinate_row_to_jarvis_row(prow)

    assert out["progress"] == 0
    assert out["progress_message"] is None
    assert out["result"] is None
    assert out["error"] is None


@pytest.mark.asyncio
async def test_get_procrastinate_job_includes_progress_join():
    """The primary SELECT must LEFT JOIN job_progress and project both columns."""
    from jarvis_common.jobs import get_procrastinate_job_for_jarvis_id

    fake_row = {
        "id": 42,
        "queue_name": "paper_ingestion",
        "task_name": "paper.process",
        "status": "doing",
        "args": {"job_id": "abc-123"},
        "progress": 0.5,
        "progress_message": "halfway",
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
    sql = conn.fetchrow.await_args.args[0]
    assert "LEFT JOIN job_progress" in sql
    assert "jp.progress" in sql
    assert "jp.message" in sql
    assert "jp.result" in sql
    assert "jp.error" in sql
    assert "args->>'job_id'" in sql


@pytest.mark.asyncio
async def test_get_procrastinate_job_falls_back_when_progress_table_missing():
    """If job_progress is missing (mig 054 not applied), retry without the JOIN."""
    import asyncpg as _asyncpg
    from jarvis_common.jobs import get_procrastinate_job_for_jarvis_id

    fallback_row = {
        "id": 42,
        "queue_name": "paper_ingestion",
        "task_name": "paper.process",
        "status": "doing",
        "args": {"job_id": "abc-123"},
        "progress": None,
        "progress_message": None,
    }

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            _asyncpg.UndefinedTableError('relation "job_progress" does not exist'),
            fallback_row,
        ]
    )
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=cm)

    result = await get_procrastinate_job_for_jarvis_id(pool, "abc-123")

    assert result == fallback_row
    # Both SELECTs must have been attempted, the second without the JOIN.
    assert conn.fetchrow.await_count == 2
    fallback_sql = conn.fetchrow.await_args_list[1].args[0]
    assert "LEFT JOIN job_progress" not in fallback_sql
    assert "NULL::REAL AS progress" in fallback_sql
    assert "NULL::jsonb AS result" in fallback_sql
    assert "NULL::jsonb AS error" in fallback_sql


@pytest.mark.asyncio
async def test_get_procrastinate_job_returns_none_when_procrastinate_table_missing():
    """If procrastinate_jobs itself is missing (mig 052 not applied), return None.

    The pool.acquire context manager raises UndefinedTableError, which is
    distinct from the per-fetchrow JOIN-missing fallback path.
    """
    import asyncpg as _asyncpg
    from jarvis_common.jobs import get_procrastinate_job_for_jarvis_id

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(side_effect=_asyncpg.UndefinedTableError("procrastinate_jobs gone"))
    cm.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=cm)

    result = await get_procrastinate_job_for_jarvis_id(pool, "abc-123")

    assert result is None


# ---------------------------------------------------------------------------
# JobStatusResponse model type correctness (migrated from test_models_jobstatus.py)
# ---------------------------------------------------------------------------


def test_job_status_response_user_id_accepts_int():
    """JobStatusResponse.user_id accepts int (from JSONB job args)."""
    from jarvis_common.models import JobStatusResponse

    response = JobStatusResponse(
        id="job-123",
        kind="paper.summarize",
        status="done",
        user_id=1,
    )
    assert response.user_id == 1
    assert isinstance(response.user_id, int)


def test_job_status_response_user_id_accepts_none():
    """JobStatusResponse.user_id accepts None."""
    from jarvis_common.models import JobStatusResponse

    response = JobStatusResponse(
        id="job-123",
        kind="paper.summarize",
        status="done",
        user_id=None,
    )
    assert response.user_id is None


def test_job_status_response_user_id_rejects_string():
    """JobStatusResponse.user_id does not accept string (it's an int field)."""
    from jarvis_common.models import JobStatusResponse
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        JobStatusResponse(
            id="job-123",
            kind="paper.summarize",
            status="done",
            user_id="not-an-int",
        )
