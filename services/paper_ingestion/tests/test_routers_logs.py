"""Tests for /api/logs/* endpoints.

Covers all six routes:
  - GET /api/logs/events          (list with filters + cursor pagination)
  - GET /api/logs/events/{id}     (single lookup + 404)
  - GET /api/logs/summary         (24h counts)
  - GET /api/logs/correlation/{id}(events for one trace, ordered ASC)
  - GET /api/logs/sources         (distinct sources, 60s cache)
  - GET /api/logs/stream/{id}     (SSE replay + job-terminal teardown)
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from httpx import ASGITransport

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CID = str(uuid.uuid4())
_CID_UUID = uuid.UUID(_CID)


def _make_pool(conn: AsyncMock) -> MagicMock:
    """Wrap *conn* in a mock asyncpg pool (acquire context manager)."""
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx
    return pool


def _event_row(
    *,
    id: int = 1,
    level: str = "info",
    category: str = "auth",
    source: str = "paper_ingestion",
    message: str = "test message",
    correlation_id: uuid.UUID | None = None,
) -> dict:
    """Return a fake asyncpg Record dict for system_events."""
    return {
        "id": id,
        "created_at": None,  # kept None for simplicity; _row_to_dict handles None
        "level": level,
        "category": category,
        "source": source,
        "message": message,
        "context": {},
        "correlation_id": correlation_id,
    }


# ---------------------------------------------------------------------------
# App fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def app_with_pool(mock_db):
    """Return (app, pool, conn) with auth + rate-limiter bypassed."""
    from jarvis_common.auth import verify_api_key
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app

    pool, conn = mock_db
    app.state.db_pool = pool
    app.state.limiter.enabled = False
    app.dependency_overrides[get_db_pool] = lambda: pool
    app.dependency_overrides[verify_api_key] = lambda: None
    yield app, pool, conn
    app.dependency_overrides.clear()
    app.state.limiter.enabled = True


# ---------------------------------------------------------------------------
# 1. list_events  — filters
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_events_filters_by_level_category_source(app_with_pool):
    """GET /api/logs/events passes level/category/source as WHERE clauses."""
    app, _pool, conn = app_with_pool

    row = _event_row(id=5, level="error", category="security", source="telegram_bot")
    conn.fetch = AsyncMock(return_value=[row])

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/api/logs/events",
            params={"level": "error", "category": "security", "source": "telegram_bot"},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "events" in body
    assert body["next_cursor"] is None
    assert len(body["events"]) == 1
    ev = body["events"][0]
    assert ev["id"] == 5
    assert ev["level"] == "error"
    assert ev["category"] == "security"
    assert ev["source"] == "telegram_bot"

    # Verify the SQL query received the filter values
    call_args = conn.fetch.await_args
    sql = call_args.args[0]
    positional = list(call_args.args[1:])
    assert "level" in sql
    assert "error" in positional
    assert "security" in positional
    assert "telegram_bot" in positional


# ---------------------------------------------------------------------------
# 2. list_events  — cursor pagination
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_events_paginates_via_cursor(app_with_pool):
    """When the DB returns limit+1 rows the next_cursor is set to last id."""
    app, _pool, conn = app_with_pool

    # Return limit+1=3 rows to trigger pagination (default limit=2 in this call)
    rows = [_event_row(id=10), _event_row(id=9), _event_row(id=8)]
    conn.fetch = AsyncMock(return_value=rows)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/logs/events", params={"limit": 2})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["events"]) == 2
    # next_cursor == id of last returned event (rows[1], id=9)
    assert body["next_cursor"] == 9

    # Second page: cursor=9 should appear in the SQL as id < 9
    conn.fetch = AsyncMock(return_value=[_event_row(id=8)])
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp2 = await client.get("/api/logs/events", params={"limit": 2, "cursor": 9})

    assert resp2.status_code == 200, resp2.text
    body2 = resp2.json()
    assert body2["next_cursor"] is None
    sql2 = conn.fetch.await_args.args[0]
    assert "id <" in sql2.replace("\n", " ")


# ---------------------------------------------------------------------------
# 3. get_event — 404 when missing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_event_by_id_returns_404_when_missing(app_with_pool):
    """GET /api/logs/events/{id} returns 404 when fetchrow is None."""
    app, _pool, conn = app_with_pool
    conn.fetchrow = AsyncMock(return_value=None)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/logs/events/9999")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Event not found"


@pytest.mark.asyncio
async def test_get_event_by_id_returns_event_when_found(app_with_pool):
    """GET /api/logs/events/{id} returns the event dict when found."""
    app, _pool, conn = app_with_pool
    conn.fetchrow = AsyncMock(return_value=_event_row(id=42, message="hello"))

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/logs/events/42")

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["id"] == 42
    assert data["message"] == "hello"


# ---------------------------------------------------------------------------
# 4. summary — counts by level and category
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summary_returns_counts_by_level_and_category(app_with_pool):
    """GET /api/logs/summary aggregates level/category counts correctly."""
    app, _pool, conn = app_with_pool
    conn.fetch = AsyncMock(
        return_value=[
            {"level": "error", "category": "auth", "n": 3},
            {"level": "warning", "category": "auth", "n": 5},
            {"level": "error", "category": "security", "n": 2},
        ]
    )

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/logs/summary")

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total"] == 10
    assert data["by_level"]["error"] == 5  # 3 + 2
    assert data["by_level"]["warning"] == 5
    assert data["by_category"]["auth"] == 8  # 3 + 5
    assert data["by_category"]["security"] == 2


# ---------------------------------------------------------------------------
# 5. correlation endpoint — ordered by created_at ASC
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_correlation_endpoint_orders_by_created_at_asc(app_with_pool):
    """GET /api/logs/correlation/{id} passes ORDER BY created_at ASC and returns events."""
    app, _pool, conn = app_with_pool

    rows = [
        _event_row(id=1, message="first", correlation_id=_CID_UUID),
        _event_row(id=2, message="second", correlation_id=_CID_UUID),
    ]
    conn.fetch = AsyncMock(return_value=rows)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/api/logs/correlation/{_CID}")

    assert resp.status_code == 200, resp.text
    events = resp.json()
    assert len(events) == 2
    assert events[0]["message"] == "first"
    assert events[1]["message"] == "second"

    # Confirm ASC ordering in SQL
    sql = conn.fetch.await_args.args[0]
    assert "ASC" in sql.upper()


# ---------------------------------------------------------------------------
# 6. sources endpoint — distinct sources
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sources_endpoint_returns_distinct_sources(app_with_pool):
    """GET /api/logs/sources returns a list of distinct source strings."""
    import paper_ingestion.routers.logs as logs_module

    app, _pool, conn = app_with_pool
    conn.fetch = AsyncMock(
        return_value=[
            {"source": "paper_ingestion"},
            {"source": "telegram_bot"},
        ]
    )

    # Clear cache so we get a fresh DB hit
    logs_module._sources_cache = None

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/logs/sources")

    assert resp.status_code == 200, resp.text
    sources = resp.json()
    assert "paper_ingestion" in sources
    assert "telegram_bot" in sources


# ---------------------------------------------------------------------------
# 7. sources endpoint — cache
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sources_endpoint_caches_for_60_seconds(app_with_pool):
    """Sources are returned from cache on the second request within TTL."""
    import paper_ingestion.routers.logs as logs_module

    app, _pool, conn = app_with_pool
    conn.fetch = AsyncMock(return_value=[{"source": "cached_source"}])

    # Seed cache manually so first request hits it
    logs_module._sources_cache = None

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        # First request populates cache
        resp1 = await client.get("/api/logs/sources")
        assert resp1.status_code == 200
        assert conn.fetch.await_count == 1

        # Second request within TTL should NOT re-query DB
        resp2 = await client.get("/api/logs/sources")
        assert resp2.status_code == 200
        assert conn.fetch.await_count == 1  # still 1 — served from cache

    # Expire cache by setting cached_at to past
    logs_module._sources_cache = (0.0, ["cached_source"])  # epoch = expired
    conn.fetch = AsyncMock(return_value=[{"source": "fresh_source"}])

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp3 = await client.get("/api/logs/sources")
        assert resp3.status_code == 200
        assert "fresh_source" in resp3.json()
        assert conn.fetch.await_count == 1  # DB re-queried after expiry


# ---------------------------------------------------------------------------
# 8. SSE stream — replay missed events with since param
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sse_stream_replays_missed_events_with_since_param(app_with_pool):
    """GET /api/logs/stream/{cid}?since=N replays events with id > N."""
    import paper_ingestion.routers.logs as logs_module

    app, _pool, conn = app_with_pool

    replay_rows = [
        _event_row(id=10, message="replayed event", correlation_id=_CID_UUID),
    ]

    fetch_call_count = 0

    async def _fetch_side_effect(sql, *args, **kwargs):
        nonlocal fetch_call_count
        fetch_call_count += 1
        if "system_events" in sql and "id >" in sql:
            return replay_rows if fetch_call_count <= 1 else []
        return []

    async def _fetchrow_side_effect(sql, *args, **kwargs):
        # No associated job found
        return None

    conn.fetch = _fetch_side_effect
    conn.fetchrow = _fetchrow_side_effect

    # Override poll interval + idle timeout to terminate quickly
    original_poll = logs_module._STREAM_POLL_INTERVAL
    original_idle = logs_module._STREAM_MAX_IDLE_SECONDS
    logs_module._STREAM_POLL_INTERVAL = 0.01
    logs_module._STREAM_MAX_IDLE_SECONDS = 0.05  # 50ms idle → terminates fast

    try:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                f"/api/logs/stream/{_CID}",
                params={"since": 9},
                timeout=5.0,
            )

        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        body = resp.text
        # The replayed event should be present in the SSE stream
        assert "replayed event" in body
        # The stream should eventually emit a done frame
        assert "event: done" in body
    finally:
        logs_module._STREAM_POLL_INTERVAL = original_poll
        logs_module._STREAM_MAX_IDLE_SECONDS = original_idle


# ---------------------------------------------------------------------------
# 9. SSE stream — terminates when correlation job finishes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sse_stream_terminates_when_correlation_job_finishes(app_with_pool):
    """Stream sends 'event: done' and closes when associated job reaches terminal status."""
    import paper_ingestion.routers.logs as logs_module

    app, _pool, conn = app_with_pool

    async def _fetch_side_effect(sql, *args, **kwargs):
        # No new events in the stream
        return []

    async def _fetchrow_side_effect(sql, *args, **kwargs):
        # Associated job is in terminal state immediately
        return {"status": "succeeded"}

    conn.fetch = _fetch_side_effect
    conn.fetchrow = _fetchrow_side_effect

    original_poll = logs_module._STREAM_POLL_INTERVAL
    logs_module._STREAM_POLL_INTERVAL = 0.01

    try:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                f"/api/logs/stream/{_CID}",
                timeout=5.0,
            )

        assert resp.status_code == 200
        body = resp.text
        # Should contain a done event with job_terminal reason
        assert "event: done" in body
        done_data_line = next(
            (
                line
                for line in body.splitlines()
                if line.startswith("data:") and "job_terminal" in line
            ),
            None,
        )
        assert done_data_line is not None, f"No job_terminal done frame in:\n{body}"
        payload = json.loads(done_data_line.removeprefix("data: "))
        assert payload["reason"] == "job_terminal"
        assert payload["status"] == "succeeded"
    finally:
        logs_module._STREAM_POLL_INTERVAL = original_poll
