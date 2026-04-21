"""Integration test for the SSE ``/api/jobs/{id}/stream`` endpoint.

Exercises the full round-trip:

1. POST /api/jobs with ``kind='noop.test'`` (DEV_MODE-gated handler)
2. A worker_loop fixture picks up the job and drives it to ``succeeded``
3. GET /api/jobs/{id}/stream yields at least one progress frame plus a
   terminal frame with ``status='succeeded'`` within a reasonable timeout.

Requires TEST_DATABASE_URL to point at a real Postgres instance.  Skipped
otherwise.  Set DEV_MODE=true BEFORE import so the ``noop.test`` handler
registers at module import time (see libs/jarvis_common/jarvis_common/jobs.py).
"""

from __future__ import annotations

import os

# NOTE: Must be set BEFORE importing jarvis_common.jobs so the gated
# ``noop.test`` handler registers at module import time.
os.environ.setdefault("JARVIS_ENABLE_TEST_JOBS", "1")

import asyncio
import json
import uuid
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

# ---------------------------------------------------------------------------
# Conditional skip if no real DB is available
# ---------------------------------------------------------------------------

_DB_URL = os.environ.get("TEST_DATABASE_URL", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _DB_URL, reason="TEST_DATABASE_URL not set — skipping live DB tests"),
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="function")
async def db_pool():
    """Create a real asyncpg pool, ensure a fresh jobs table, yield, teardown."""
    import asyncpg
    from jarvis_common import init_pg_connection

    try:
        pool = await asyncpg.create_pool(
            _DB_URL,
            min_size=1,
            max_size=4,
            init=init_pg_connection,
        )
    except Exception as exc:  # pragma: no cover - skip when DB unreachable
        pytest.skip(f"Cannot connect to test DB: {exc}")
        return  # unreachable, appeases type checker

    # Ensure the jobs table exists (applied by migration 023; create if missing).
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
        await conn.execute("DELETE FROM jobs")

    yield pool

    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM jobs")
    await pool.close()


@pytest.fixture()
def app_with_pool(db_pool):
    """Wire the real db_pool into the FastAPI app and bypass auth / rate-limit."""
    from jarvis_common import current_user_id, verify_api_key
    from paper_ingestion.main import app

    app.state.db_pool = db_pool
    try:
        app.state.limiter.enabled = False
    except AttributeError:
        pass

    # Bypass API key auth and default current_user_id to None (single-tenant).
    app.dependency_overrides[verify_api_key] = lambda: None
    app.dependency_overrides[current_user_id] = lambda: None

    yield app

    app.dependency_overrides.clear()
    app.state.limiter.enabled = True


@pytest_asyncio.fixture()
async def worker_task(db_pool):
    """Background worker_loop that claims and runs ``noop.test`` jobs."""
    import httpx
    from jarvis_common.jobs import worker_loop

    http_mock = AsyncMock(spec=httpx.AsyncClient)
    stop_event = asyncio.Event()

    async def _run():
        await worker_loop(
            db_pool,
            http_mock,
            kinds={"noop.test"},
            poll_interval=0.1,
            stop_event=stop_event,
        )

    task = asyncio.create_task(_run())
    try:
        yield task
    finally:
        stop_event.set()
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except (TimeoutError, asyncio.CancelledError):
            task.cancel()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_sse_events(raw: str) -> list[dict]:
    """Parse an SSE response body into a list of parsed JSON event dicts.

    The paper_ingestion stream endpoint emits unnamed ``data:`` frames, so we
    only split on blank lines and decode each ``data:`` payload as JSON.
    """
    events: list[dict] = []
    for frame in raw.split("\n\n"):
        frame = frame.strip()
        if not frame:
            continue
        data_lines = [
            line[len("data:") :].strip() for line in frame.splitlines() if line.startswith("data:")
        ]
        if not data_lines:
            continue
        payload = "\n".join(data_lines)
        try:
            events.append(json.loads(payload))
        except json.JSONDecodeError:
            # Skip non-JSON payloads (keepalives, comments, etc.)
            continue
    return events


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sse_stream_noop_roundtrip(app_with_pool, worker_task) -> None:
    """POST /api/jobs (noop.test) → worker runs it → SSE yields succeeded frame."""
    import httpx
    from httpx import ASGITransport

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app_with_pool), base_url="http://test"
    ) as client:
        # 1. Enqueue a noop.test job
        create_resp = await client.post(
            "/api/jobs",
            json={"kind": "noop.test", "payload": {"marker": "integ"}},
        )
        assert create_resp.status_code == 201, (
            f"POST /api/jobs failed: {create_resp.status_code} {create_resp.text}"
        )
        job_id = create_resp.json()["job_id"]
        uuid.UUID(job_id)  # sanity-check

        # 2. Stream updates, bounded by a 10 s deadline.
        events: list[dict] = []

        async def _consume() -> None:
            async with client.stream("GET", f"/api/jobs/{job_id}/stream") as resp:
                assert resp.status_code == 200, (
                    f"SSE endpoint returned {resp.status_code}: "
                    f"{(await resp.aread()).decode(errors='replace')}"
                )
                buffer = ""
                async for chunk in resp.aiter_text():
                    buffer += chunk
                    # Yield parsed events as soon as a full frame arrives.
                    while "\n\n" in buffer:
                        frame, buffer = buffer.split("\n\n", 1)
                        parsed = _parse_sse_events(frame + "\n\n")
                        events.extend(parsed)
                        # Terminal status → stop reading.
                        if any(
                            e.get("status") in {"succeeded", "failed", "cancelled"} for e in parsed
                        ):
                            return

        try:
            await asyncio.wait_for(_consume(), timeout=10.0)
        except TimeoutError:  # pragma: no cover - diagnostic branch
            from jarvis_common.jobs import get as _get

            row = await _get(app_with_pool.state.db_pool, job_id)
            pytest.fail(
                "SSE stream did not close within 10 s. "
                f"Job row status={row and row['status']}, events so far={events}"
            )

    # 3. Assert we saw at least one non-terminal progress frame and a terminal
    #    succeeded frame.
    terminal_statuses = {"succeeded", "failed", "cancelled"}
    terminal_events = [e for e in events if e.get("status") in terminal_statuses]
    assert terminal_events, f"No terminal event received; events={events}"

    final = terminal_events[-1]
    assert final["status"] == "succeeded", (
        f"Expected terminal status 'succeeded', got {final['status']}: {final}"
    )
    # Result payload from the noop handler
    assert final.get("result") == {"ok": True, "echo": {"marker": "integ"}}, (
        f"Unexpected result payload: {final.get('result')}"
    )

    # At least one progress frame (either pre-terminal or the terminal one
    # with progress=1.0 — the noop handler emits 0.5 then the runner sets 1.0).
    progress_values: list[float] = [p for e in events if (p := e.get("progress")) is not None]
    assert progress_values, f"No progress frames emitted; events={events}"
    assert max(progress_values) == 1.0, f"Expected a frame with progress=1.0, saw {progress_values}"
