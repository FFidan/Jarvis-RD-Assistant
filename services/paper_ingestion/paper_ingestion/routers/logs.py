"""Log/event-stream endpoints for system_events table.

Provides cursor-paginated listing, single-event lookup, 24h summary,
correlation-id grouping, source enumeration, and an SSE stream for
live-tailing events by correlation_id.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid as uuid_mod
from collections.abc import AsyncGenerator
from datetime import datetime
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from jarvis_common.auth import require_admin_or_api_key, verify_api_key
from jarvis_common.db_helpers import escape_like
from jarvis_common.sse import sse_event, sse_keepalive
from starlette.responses import StreamingResponse

from paper_ingestion.deps import get_db_pool, limiter

router = APIRouter(
    prefix="/api/logs",
    tags=["logs"],
    dependencies=[Depends(verify_api_key), Depends(require_admin_or_api_key)],
)

# ---------------------------------------------------------------------------
# In-process sources cache  (module-scope, guarded by asyncio.Lock)
# ---------------------------------------------------------------------------

_sources_cache: tuple[float, list[str]] | None = None
_sources_lock: asyncio.Lock | None = None
_SOURCES_TTL: float = 60.0  # seconds; set to 0 in tests to bypass cache


def _get_sources_lock() -> asyncio.Lock:
    global _sources_lock
    if _sources_lock is None:
        _sources_lock = asyncio.Lock()
    return _sources_lock


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _row_to_dict(row: Any) -> dict[str, Any]:
    """Convert an asyncpg Record to a JSON-serialisable dict."""
    d = dict(row)
    # created_at → ISO string
    if "created_at" in d and d["created_at"] is not None:
        v = d["created_at"]
        d["created_at"] = v.isoformat() if hasattr(v, "isoformat") else str(v)
    # correlation_id → string
    if "correlation_id" in d and d["correlation_id"] is not None:
        d["correlation_id"] = str(d["correlation_id"])
    # context is already decoded by asyncpg JSONB codec — leave as-is
    return d


# ---------------------------------------------------------------------------
# GET /api/logs/events  — cursor-paginated list with filters
# ---------------------------------------------------------------------------


@router.get("/events")
@limiter.limit("60/minute")
async def list_events(
    request: Request,
    level: str | None = None,
    category: str | None = None,
    source: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    cursor: int | None = None,
    limit: int = Query(50, ge=1, le=200),
    q: str | None = Query(default=None, max_length=500),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> dict[str, Any]:
    """Return up to *limit* system_events ordered by id DESC with optional filters.

    Cursor pagination: pass ``cursor=<last id>`` to get the next page.
    """
    conditions: list[str] = []
    params: list[Any] = []
    idx = 1

    if cursor is not None:
        conditions.append(f"id < ${idx}")
        params.append(cursor)
        idx += 1

    if level is not None:
        conditions.append(f"level = ${idx}")
        params.append(level)
        idx += 1

    if category is not None:
        conditions.append(f"category = ${idx}")
        params.append(category)
        idx += 1

    if source is not None:
        conditions.append(f"source = ${idx}")
        params.append(source)
        idx += 1

    if since is not None:
        conditions.append(f"created_at >= ${idx}")
        params.append(since)
        idx += 1

    if until is not None:
        conditions.append(f"created_at <= ${idx}")
        params.append(until)
        idx += 1

    if q is not None:
        # PI-SEC-02: escape LIKE metacharacters in the user term and pair the
        # predicate with ESCAPE '\' so '%'/'_' match literally instead of acting
        # as wildcards (information disclosure / full-scan DoS).
        conditions.append(f"message ILIKE ${idx} ESCAPE '\\'")
        params.append(f"%{escape_like(q)}%")
        idx += 1

    where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit + 1)
    fetch_limit_param = idx

    sql = f"""
        SELECT id, created_at, level, category, source, message, context, correlation_id
        FROM system_events
        {where_clause}
        ORDER BY id DESC
        LIMIT ${fetch_limit_param}
    """

    async with db_pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)

    events = [_row_to_dict(r) for r in rows[:limit]]
    next_cursor: int | None = None
    if len(rows) > limit:
        next_cursor = events[-1]["id"]

    return {"events": events, "next_cursor": next_cursor}


# ---------------------------------------------------------------------------
# GET /api/logs/events/{id}  — single event lookup
# ---------------------------------------------------------------------------


@router.get("/events/{event_id}")
@limiter.limit("120/minute")
async def get_event(
    event_id: int,
    request: Request,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> dict[str, Any]:
    """Return a single system_event by primary key. 404 if not found."""
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, created_at, level, category, source, message, context, correlation_id
            FROM system_events
            WHERE id = $1
            """,
            event_id,
        )
    if row is None:
        raise HTTPException(status_code=404, detail="Event not found")
    return _row_to_dict(row)


# ---------------------------------------------------------------------------
# GET /api/logs/summary  — counts by level / category for last 24 h
# ---------------------------------------------------------------------------


@router.get("/summary")
@limiter.limit("30/minute")
async def get_summary(
    request: Request,
    exclude_infra: bool = Query(False, alias="exclude_infra"),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> dict[str, Any]:
    """Return event counts by level and category for the last 24 hours.

    Pass ``?exclude_infra=1`` to omit ``category='infra'`` events from the
    counts — used by the header error-badge so nginx rate-limit 503s
    (self-inflicted infra noise) don't inflate the user-visible error count.
    """
    where = "WHERE created_at >= NOW() - INTERVAL '24 hours'"
    if exclude_infra:
        where += " AND category != 'infra'"

    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT level, category, COUNT(*) AS n
            FROM system_events
            {where}
            GROUP BY level, category
            """
        )

    by_level: dict[str, int] = {}
    by_category: dict[str, int] = {}
    total = 0

    for row in rows:
        n = int(row["n"])
        total += n
        lv = row["level"]
        cat = row["category"]
        if lv is not None:
            by_level[lv] = by_level.get(lv, 0) + n
        if cat is not None:
            by_category[cat] = by_category.get(cat, 0) + n

    return {"by_level": by_level, "by_category": by_category, "total": total}


# ---------------------------------------------------------------------------
# GET /api/logs/correlation/{correlation_id}  — events for one trace
# ---------------------------------------------------------------------------


@router.get("/correlation/{correlation_id}")
@limiter.limit("60/minute")
async def get_correlation(
    correlation_id: str,
    request: Request,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> list[dict[str, Any]]:
    """Return all events for a correlation_id ordered by created_at ASC."""
    try:
        cid = uuid_mod.UUID(correlation_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="correlation_id must be a valid UUID")

    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, created_at, level, category, source, message, context, correlation_id
            FROM system_events
            WHERE correlation_id = $1
            ORDER BY created_at ASC
            """,
            cid,
        )
    return [_row_to_dict(r) for r in rows]


# ---------------------------------------------------------------------------
# GET /api/logs/sources  — DISTINCT source values (60s in-process cache)
# ---------------------------------------------------------------------------


@router.get("/sources")
@limiter.limit("30/minute")
async def list_sources(
    request: Request,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> list[str]:
    """Return DISTINCT source values seen in the last 7 days.

    Results are cached in-process for 60 seconds.
    """
    global _sources_cache

    lock = _get_sources_lock()
    async with lock:
        now = time.monotonic()
        if _sources_cache is not None:
            cached_at, cached_sources = _sources_cache
            if now - cached_at < _SOURCES_TTL:
                return cached_sources

        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT DISTINCT source
                FROM system_events
                WHERE created_at >= NOW() - INTERVAL '7 days'
                  AND source IS NOT NULL
                ORDER BY source
                """
            )
        sources = [row["source"] for row in rows]
        _sources_cache = (now, sources)
        return sources


# ---------------------------------------------------------------------------
# GET /api/logs/stream/{correlation_id}  — live SSE tail
# ---------------------------------------------------------------------------

_STREAM_POLL_INTERVAL: float = 1.0  # seconds between DB polls
_STREAM_MAX_IDLE_SECONDS: float = 1800.0  # 30-minute idle timeout


async def _get_associated_job_status(
    conn: Any,
    correlation_id: uuid_mod.UUID,
) -> str | None:
    """Return the procrastinate job status for the job linked to this correlation_id.

    Returns None if no associated job is found.
    """
    row = await conn.fetchrow(
        """
        SELECT pj.status
        FROM procrastinate_jobs pj
        WHERE pj.id = (
            SELECT (context->>'job_id')::int
            FROM system_events
            WHERE correlation_id = $1
              AND message = 'started'
            LIMIT 1
        )
        """,
        correlation_id,
    )
    if row is None:
        return None
    return row["status"]


_TERMINAL_JOB_STATUSES = frozenset({"succeeded", "failed", "cancelled"})


async def _stream_correlation_events(
    db_pool: asyncpg.Pool,
    correlation_id: uuid_mod.UUID,
    since_id: int,
) -> AsyncGenerator[str, None]:  # type: ignore[type-arg]
    """Async generator that polls system_events and yields SSE frames."""
    last_id = since_id
    idle_since = time.monotonic()

    while True:
        await asyncio.sleep(_STREAM_POLL_INTERVAL)

        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, created_at, level, category, source, message, context, correlation_id
                FROM system_events
                WHERE correlation_id = $1
                  AND id > $2
                ORDER BY id ASC
                """,
                correlation_id,
                last_id,
            )

            # Check associated job terminal status
            job_status = await _get_associated_job_status(conn, correlation_id)

        if rows:
            idle_since = time.monotonic()
            for row in rows:
                payload = _row_to_dict(row)
                yield sse_event(payload)
                last_id = row["id"]

        # Terminate if job is in a terminal state
        if job_status in _TERMINAL_JOB_STATUSES:
            done_payload = json.dumps({"reason": "job_terminal", "status": job_status})
            yield f"event: done\ndata: {done_payload}\n\n"
            return

        # Terminate on idle timeout
        if time.monotonic() - idle_since > _STREAM_MAX_IDLE_SECONDS:
            yield f"event: done\ndata: {json.dumps({'reason': 'idle_timeout'})}\n\n"
            return

        # Keepalive comment every poll cycle (low overhead)
        yield sse_keepalive()


@router.get("/stream/{correlation_id}")
@limiter.limit("20/minute")
async def stream_correlation(
    correlation_id: str,
    request: Request,
    since: int = Query(0, description="Resume from this event id (0 = start from now)"),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
):
    """Stream SSE events for a correlation_id.

    Clients may pass ``?since=<event_id>`` to replay missed events on
    reconnect.  The stream terminates when the associated job reaches a
    terminal state or after 30 minutes of idle time.
    """
    try:
        cid = uuid_mod.UUID(correlation_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="correlation_id must be a valid UUID")

    return StreamingResponse(
        _stream_correlation_events(db_pool, cid, since),
        media_type="text/event-stream",
    )
