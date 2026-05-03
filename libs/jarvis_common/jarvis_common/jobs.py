"""Async job queue backbone shared by all JARVIS microservices.

Provides:
- ``@job_handler`` decorator to register handlers by kind name.
- ``JobContext`` for handler-side progress reporting and cancellation checks.
- ``JobError`` for structured errors with an optional action_link payload.
- ``enqueue``, ``get``, ``list_jobs``, ``request_cancel`` — DB helpers.
- ``run_job`` — atomically claims and executes one queued job.
- ``worker_loop`` — background polling loop; mount in service lifespan.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

import asyncpg
import asyncpg_listen
import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Job-handler ownership map (documentation only — not runtime-enforced)
# ---------------------------------------------------------------------------
#
# Contract:
#   - Each ``@job_handler`` kind is registered by exactly ONE service's worker
#     loop (the OWNER listed below).  The owner's ``worker_loop`` will dequeue
#     and execute jobs of that kind.
#   - Any service may ENQUEUE a job of any kind via ``jobs_lib.enqueue``.
#     The enqueuing service does NOT need to own the handler — it just writes
#     a row to the ``jobs`` table and the owner's worker will pick it up.
#   - This mapping is informational.  Adding a kind here does NOT register a
#     handler; you must still decorate the function with ``@job_handler``.
#
# Ownership map (grep for ``@job_handler(`` to keep this in sync):
JOB_HANDLER_OWNER: dict[str, Literal["paper_ingestion", "learning_engine", "telegram_bot"]] = {
    # paper_ingestion handlers
    "paper.process": "paper_ingestion",
    "paper.analyze": "paper_ingestion",
    "papers.batch_process": "paper_ingestion",
    "papers.batch_summarize": "paper_ingestion",
    "papers.scan_local": "paper_ingestion",
    "paper.summarize": "paper_ingestion",
    "citations.batch_fetch": "paper_ingestion",
    "digest.weekly": "paper_ingestion",
    "extraction.single": "paper_ingestion",
    "extraction.batch": "paper_ingestion",
    "contradictions.scan": "paper_ingestion",
    "pulse.generate": "paper_ingestion",
    "pulse.train_classifier": "paper_ingestion",
    "zotero.push": "paper_ingestion",
    "zotero.resync": "paper_ingestion",
    "zotero.sync_from_zotero": "paper_ingestion",
    "zotero.sync_annotations": "paper_ingestion",
    # learning_engine handlers
    "card.generate": "learning_engine",
    "card.generate_batch": "learning_engine",
}

# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class ProgressContext(Protocol):
    """Minimum interface a job handler receives as its execution context."""

    async def report_progress(self, percent: float, message: str = "") -> None: ...


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

HandlerFn = Callable[
    [asyncpg.Pool, httpx.AsyncClient, dict[str, Any], "JobContext"],
    Awaitable[dict[str, Any]],
]

# SSE keepalive / max-stream constants shared across routers
KEEPALIVE_INTERVAL = 15.0  # seconds between keepalive comments
MAX_STREAM_SECONDS = 750  # hard ceiling; yields streaming_timeout and exits
JOB_NOTIFY_CHANNEL = "jarvis_jobs"
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})

# B.4 Step 2: procrastinate broker bridge.
# The procrastinate schema (migration 052) emits NOTIFY on this channel for
# job inserts and abort_requested events. There is NO procrastinate trigger
# for status transitions (todo→doing→succeeded/failed) — those are observed
# via polling the procrastinate_jobs table on each stream cycle.
PROCRASTINATE_NOTIFY_CHANNEL = "procrastinate_any_queue_v1"

# Map procrastinate's status enum → JARVIS legacy status strings.
# Source: db/migrations/052_procrastinate_schema.sql:27-35.
PROCRASTINATE_STATUS_MAP: dict[str, str] = {
    "todo": "queued",
    "doing": "running",
    "succeeded": "succeeded",
    "failed": "failed",
    "cancelled": "cancelled",
    "aborting": "running",  # legacy, not used since procrastinate v3.0.0
    "aborted": "cancelled",
}

# Backward-compatible aliases (deprecated — import the public names instead)
_KEEPALIVE_INTERVAL = KEEPALIVE_INTERVAL
_MAX_STREAM_SECONDS = MAX_STREAM_SECONDS

_HANDLERS: dict[str, HandlerFn] = {}

# Strip ANSI escape codes and absolute paths from error messages before persisting.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_PATH_RE = re.compile(r"(/[^\s]+)+")


class _PoolListenConnection:
    """Adapter letting asyncpg-listen borrow and release a pooled connection."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool
        self._conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy | None = None
        self._listeners: list[tuple[str, Callable[..., None]]] = []
        self._released = False

    @classmethod
    async def create(cls, pool: asyncpg.Pool) -> _PoolListenConnection:
        wrapped = cls(pool)
        wrapped._conn = await pool.acquire()
        return wrapped

    async def add_listener(self, channel: str, callback: Callable[..., None]) -> None:
        if self._conn is None:
            raise RuntimeError("listen connection is not open")
        await self._conn.add_listener(channel, callback)
        self._listeners.append((channel, callback))

    async def execute(self, *args: Any, **kwargs: Any) -> str:
        if self._conn is None:
            raise RuntimeError("listen connection is not open")
        return await self._conn.execute(*args, **kwargs)

    def is_closed(self) -> bool:
        return self._released or self._conn is None or self._conn.is_closed()

    async def close(self) -> None:
        if self._conn is None or self._released:
            return
        for channel, callback in self._listeners:
            with suppress(Exception):
                await self._conn.remove_listener(channel, callback)
        await self._pool.release(self._conn)
        self._released = True
        self._conn = None


def _sanitize_error_message(raw: str) -> str:
    msg = _ANSI_RE.sub("", raw)
    msg = _PATH_RE.sub("<path>", msg)
    return msg[:500]


async def notify_job_update(
    conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy, job_id: str
) -> None:
    """Emit a best-effort PostgreSQL notification for job stream listeners."""
    try:
        await conn.execute("SELECT pg_notify($1, $2)", JOB_NOTIFY_CHANNEL, str(job_id))
    except Exception:
        logger.debug("jobs.notify failed for job %s", job_id, exc_info=True)


def job_sse_payload(
    row: dict[str, Any],
    *,
    source: Literal["legacy", "procrastinate"] = "legacy",
) -> dict[str, Any]:
    """Return the public SSE payload for a job row.

    ``source`` is a debugging discriminator that tags whether the row came
    from the legacy ``jobs`` table or from ``procrastinate_jobs`` (B.4 Step 2
    bridge). The frontend currently ignores this field — it exists so
    operators can correlate SSE events with the originating broker during
    the dual-write period.
    """
    event_data: dict[str, Any] = {
        "progress": row.get("progress"),
        "progress_message": row.get("progress_message"),
        "status": row["status"],
        "source": source,
    }
    if row["status"] in TERMINAL_STATUSES:
        if row.get("result") is not None:
            event_data["result"] = row["result"]
        if row.get("error") is not None:
            event_data["error"] = row["error"]
        if row.get("payload") is not None:
            event_data["payload"] = row["payload"]
    return event_data


def procrastinate_status_to_jarvis(procrastinate_status: str) -> str:
    """Map a procrastinate status enum value to the legacy JARVIS status string.

    Unknown statuses (e.g. future procrastinate enum additions) fall through
    to ``"running"`` so the SSE stream stays open and a future poll can
    discover the terminal state. Use ``PROCRASTINATE_STATUS_MAP`` directly
    for tests that need to detect unknown values.
    """
    return PROCRASTINATE_STATUS_MAP.get(procrastinate_status, "running")


def procrastinate_row_to_jarvis_row(prow: dict[str, Any]) -> dict[str, Any]:
    """Adapt a ``procrastinate_jobs`` row into the legacy ``jobs``-row shape.

    The legacy shape is what ``job_sse_payload`` expects: a dict with
    ``status`` (legacy enum), ``progress``, ``progress_message``, optional
    ``result`` / ``error`` / ``payload`` keys. procrastinate's row has
    ``status`` (procrastinate enum), ``args`` (kwargs JSONB), and no native
    progress columns — the bridge surfaces ``args`` as ``payload`` so the
    terminal-event payload dump stays consistent.
    """
    return {
        "status": procrastinate_status_to_jarvis(prow["status"]),
        "progress": prow.get("progress"),
        "progress_message": prow.get("progress_message"),
        "result": prow.get("result"),
        "error": prow.get("error"),
        "payload": prow.get("args"),
    }


async def get_procrastinate_job_for_jarvis_id(
    pool: asyncpg.Pool, jarvis_job_id: str
) -> dict[str, Any] | None:
    """Fetch the matching ``procrastinate_jobs`` row by ``args->>'job_id'``.

    Returns ``None`` when:
      * no procrastinate row carries this JARVIS job_id, or
      * the ``procrastinate_jobs`` table does not exist (migration 052 not
        applied — graceful degradation so legacy-only DBs still work).

    Mirrors :func:`get` for the legacy table.
    """
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, queue_name, task_name, status, args
                FROM procrastinate_jobs
                WHERE args->>'job_id' = $1
                ORDER BY id DESC
                LIMIT 1
                """,
                str(jarvis_job_id),
            )
    except asyncpg.UndefinedTableError:
        return None
    except Exception:
        logger.debug("procrastinate row lookup failed for job %s", jarvis_job_id, exc_info=True)
        return None
    if row is None:
        return None
    return dict(row)


async def _wait_for_job_notification(pool: asyncpg.Pool, job_id: str, timeout: float) -> bool:
    """Wait for a job-specific NOTIFY payload, returning False on fallback timeout.

    Subscribes to BOTH ``JOB_NOTIFY_CHANNEL`` (legacy ``jarvis_jobs``, payload
    is the job UUID) AND ``PROCRASTINATE_NOTIFY_CHANNEL`` (B.4 bridge,
    payload is a procrastinate JSON envelope ``{"type": ..., "job_id": ...}``).

    For the legacy channel: payload-equality check on the job UUID.
    For the procrastinate channel: any notify is treated as a wake-up so the
    polling loop re-queries ``procrastinate_jobs`` — the procrastinate
    triggers don't carry the JARVIS job_id, only the procrastinate bigint id,
    so a precise filter would require an extra DB lookup. A wake-up false
    positive just means an extra poll, which is cheap.
    """
    matched = asyncio.Event()

    async def _connect() -> _PoolListenConnection:
        return await _PoolListenConnection.create(pool)

    async def _handle_legacy(
        notification: asyncpg_listen.Notification | asyncpg_listen.Timeout,
    ) -> None:
        if isinstance(notification, asyncpg_listen.Notification) and notification.payload == str(
            job_id
        ):
            matched.set()

    async def _handle_procrastinate(
        notification: asyncpg_listen.Notification | asyncpg_listen.Timeout,
    ) -> None:
        # Any procrastinate notify is a wake-up — caller will re-poll the row.
        if isinstance(notification, asyncpg_listen.Notification):
            matched.set()

    listener = asyncpg_listen.NotificationListener(_connect, reconnect_delay=timeout)  # type: ignore[arg-type]
    listen_task = asyncio.create_task(
        listener.run(
            {
                JOB_NOTIFY_CHANNEL: _handle_legacy,
                PROCRASTINATE_NOTIFY_CHANNEL: _handle_procrastinate,
            },
            policy=asyncpg_listen.ListenPolicy.ALL,
            notification_timeout=timeout,
        )
    )
    wait_task = asyncio.create_task(matched.wait())

    try:
        done, _pending = await asyncio.wait(
            {listen_task, wait_task},
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if wait_task in done:
            return wait_task.result()
        if listen_task in done:
            exc = listen_task.exception()
            if exc is not None:
                logger.debug(
                    "jobs.listen setup failed; falling back to polling",
                    exc_info=(type(exc), exc, exc.__traceback__),
                )
            return False
        return False
    finally:
        for task in (listen_task, wait_task):
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            else:
                with suppress(asyncio.CancelledError, Exception):
                    task.result()


async def stream_job_events(
    pool: asyncpg.Pool,
    job_id: str,
    *,
    is_disconnected: Callable[[], Awaitable[bool]],
) -> AsyncIterator[str]:
    """Yield SSE frames for a job, using LISTEN/NOTIFY with polling fallback.

    Subscribes to both the legacy ``jarvis_jobs`` channel and the procrastinate
    ``procrastinate_any_queue_v1`` channel (B.4 Step 2). On each cycle we poll
    BOTH the legacy ``jobs`` table and the ``procrastinate_jobs`` table; either
    path can yield a frame. The procrastinate row is normalised to the legacy
    SSE payload shape via :func:`procrastinate_row_to_jarvis_row` and tagged
    with ``source: "procrastinate"`` for debugging.

    The stream terminates when EITHER source reaches a JARVIS-terminal status
    (``succeeded`` / ``failed`` / ``cancelled``) — this preserves the legacy
    contract that the SSE response closes once the job is done. During the
    dual-write period (Step 2) only the legacy path drives terminal events
    in production; the procrastinate branch is exercised by tests today and
    becomes the primary signal once the actual enqueue path migrates in
    Group C.
    """
    last_legacy_key: tuple[Any, Any, Any] | None = None
    last_procrastinate_key: tuple[Any, Any, Any] | None = None
    loop = asyncio.get_running_loop()
    loop_start = loop.time()
    last_keepalive = loop_start

    poll_interval = 2.0
    idle_ticks = 0
    last_state: tuple[Any, Any, Any] | None = None

    while True:
        if await is_disconnected():
            logger.debug("SSE client disconnected for job %s", job_id)
            break

        now = loop.time()
        elapsed = now - loop_start

        if elapsed > MAX_STREAM_SECONDS:
            logger.warning("SSE stream timeout for job %s after %.0fs", job_id, elapsed)
            yield f"data: {json.dumps({'status': 'streaming_timeout'})}\n\n"
            break

        if now - last_keepalive >= KEEPALIVE_INTERVAL:
            yield ": keepalive\n\n"
            last_keepalive = now

        legacy_row = await get(pool, job_id)
        procrastinate_raw = await get_procrastinate_job_for_jarvis_id(pool, job_id)

        # Emit legacy frame if changed.
        if legacy_row is not None:
            legacy_key = (
                legacy_row.get("progress"),
                legacy_row.get("progress_message"),
                legacy_row["status"],
            )
            if legacy_key != last_legacy_key:
                last_legacy_key = legacy_key
                yield f"data: {json.dumps(job_sse_payload(legacy_row, source='legacy'))}\n\n"

        # Emit procrastinate frame if changed.
        procrastinate_row: dict[str, Any] | None = None
        if procrastinate_raw is not None:
            procrastinate_row = procrastinate_row_to_jarvis_row(procrastinate_raw)
            procrastinate_key = (
                procrastinate_row.get("progress"),
                procrastinate_row.get("progress_message"),
                procrastinate_row["status"],
            )
            if procrastinate_key != last_procrastinate_key:
                last_procrastinate_key = procrastinate_key
                yield (
                    "data: "
                    + json.dumps(job_sse_payload(procrastinate_row, source="procrastinate"))
                    + "\n\n"
                )

        # If the legacy row vanished AND no procrastinate row exists, the job
        # is unknown — terminate (matches pre-bridge behavior).
        if legacy_row is None and procrastinate_row is None:
            break

        # Adaptive poll throttling keys off the legacy row's state when it
        # exists; otherwise the procrastinate row drives the cadence so a
        # procrastinate-only stream still backs off when idle.
        active_row = legacy_row if legacy_row is not None else procrastinate_row
        assert active_row is not None  # narrowing — unreachable when both None (handled above)
        current_state = (
            active_row.get("progress"),
            active_row.get("progress_message"),
            active_row["status"],
        )
        if current_state != last_state:
            last_state = current_state
            idle_ticks = 0
            poll_interval = 2.0
        else:
            idle_ticks += 1
            if idle_ticks * poll_interval > 30:
                poll_interval = min(poll_interval + 1.0, 5.0)

        # Terminate as soon as EITHER source has reached a terminal state.
        legacy_terminal = legacy_row is not None and legacy_row["status"] in TERMINAL_STATUSES
        procrastinate_terminal = (
            procrastinate_row is not None and procrastinate_row["status"] in TERMINAL_STATUSES
        )
        if legacy_terminal or procrastinate_terminal:
            break

        await _wait_for_job_notification(pool, job_id, poll_interval)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class JobError(Exception):
    """Raise from a handler to produce a structured error with an optional link."""

    def __init__(self, message: str, *, action_link: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.action_link = action_link


# ---------------------------------------------------------------------------
# Decorator
# ---------------------------------------------------------------------------


def job_handler(kind: str) -> Callable[[HandlerFn], HandlerFn]:
    """Register a coroutine as the handler for jobs of the given ``kind``."""

    def decorator(fn: HandlerFn) -> HandlerFn:
        _HANDLERS[kind] = fn
        return fn

    return decorator


# ---------------------------------------------------------------------------
# JobContext
# ---------------------------------------------------------------------------


@dataclass
class JobContext:
    """Passed to each handler; allows progress reporting and cancellation checks."""

    job_id: str
    _pool: asyncpg.Pool = field(repr=False)
    _cancelled: bool = field(default=False, init=False, repr=False)

    async def update_progress(self, progress: float, message: str | None = None) -> None:
        """Persist progress (0–1) and optional human-readable message to the DB.

        Also refreshes ``last_heartbeat_at`` so the stale-job reaper does not
        kill long-running jobs that are actively reporting progress.
        """
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE jobs"
                " SET progress = $1, progress_message = $2, last_heartbeat_at = NOW()"
                " WHERE id = $3::uuid",
                progress,
                message,
                self.job_id,
            )
            await notify_job_update(conn, self.job_id)
        logger.debug("job %s progress=%.2f msg=%s", self.job_id, progress, message)

    async def is_cancelled(self) -> bool:
        """Return True if a cancel has been requested since the job started."""
        if self._cancelled:
            return True
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT cancel_requested FROM jobs WHERE id = $1::uuid",
                self.job_id,
            )
        if row and row["cancel_requested"]:
            self._cancelled = True
        return self._cancelled


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


async def enqueue(
    pool: asyncpg.Pool,
    kind: str,
    payload: dict[str, Any] | None = None,
    *,
    user_id: str | None = None,
) -> str:
    """Insert a new queued job and return its UUID string."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO jobs (kind, payload, user_id)
            VALUES ($1, $2::jsonb, $3)
            RETURNING id::text
            """,
            kind,
            payload or {},
            user_id,
        )
        if row is not None:
            await notify_job_update(conn, row["id"])
    if row is None:
        raise RuntimeError("enqueue returned no row")
    return row["id"]


async def get(pool: asyncpg.Pool, job_id: str) -> dict[str, Any] | None:
    """Fetch a single job row by UUID; returns None if not found."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM jobs WHERE id = $1::uuid",
            job_id,
        )
    if row is None:
        return None
    return dict(row)


async def list_jobs(
    pool: asyncpg.Pool,
    *,
    status: str | None = None,
    kind: str | None = None,
    limit: int = 50,
    user_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return jobs filtered by status and/or kind, newest first.

    When ``user_id`` is provided only jobs owned by that user (or jobs with a
    NULL user_id, i.e. system jobs) are returned.  Passing ``user_id=None``
    returns all jobs (single-tenant / no-ownership mode).
    """
    conditions: list[str] = []
    params: list[Any] = []
    idx = 1

    if user_id is not None:
        conditions.append(f"(user_id IS NULL OR user_id = ${idx})")
        params.append(user_id)
        idx += 1
    if status is not None:
        conditions.append(f"status = ${idx}")
        params.append(status)
        idx += 1
    if kind is not None:
        conditions.append(f"kind = ${idx}")
        params.append(kind)
        idx += 1

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit)
    query = f"SELECT * FROM jobs {where} ORDER BY created_at DESC LIMIT ${idx}"

    async with pool.acquire() as conn:
        rows = await conn.fetch(query, *params)
    return [dict(r) for r in rows]


async def request_cancel(pool: asyncpg.Pool, job_id: str) -> None:
    """Set cancel_requested=TRUE for the given job.

    If the job is still queued (not yet claimed by a worker), it is
    transitioned immediately to the terminal 'cancelled' state so that the
    worker_loop never picks it up.  Running jobs keep their status; the
    handler is expected to poll ``JobContext.is_cancelled()`` co-operatively.
    """
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE jobs
               SET cancel_requested = TRUE,
                   status = CASE WHEN status = 'queued' THEN 'cancelled' ELSE status END,
                   finished_at = CASE WHEN status = 'queued' THEN NOW() ELSE finished_at END
             WHERE id = $1::uuid
            """,
            job_id,
        )
        await notify_job_update(conn, job_id)


# ---------------------------------------------------------------------------
# Internal finish helper
# ---------------------------------------------------------------------------


async def _finish(
    pool: asyncpg.Pool,
    job_id: str,
    *,
    status: str,
    result: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE jobs
            SET status = $1,
                result = $2::jsonb,
                error  = $3::jsonb,
                finished_at = NOW(),
                progress = CASE WHEN $1 = 'succeeded' THEN 1.0 ELSE progress END
            WHERE id = $4::uuid
            """,
            status,
            result,
            error,
            job_id,
        )
        await notify_job_update(conn, job_id)


# ---------------------------------------------------------------------------
# run_job
# ---------------------------------------------------------------------------


async def run_job(
    pool: asyncpg.Pool,
    http_client: httpx.AsyncClient,
    job_id: str,
) -> None:
    """Atomically claim a queued job, execute its handler, then persist outcome.

    If the job has already been picked up by another worker the function
    returns silently (idempotent).
    """
    # Atomic queued → running transition
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE jobs
            SET status = 'running', started_at = NOW()
            WHERE id = $1::uuid AND status = 'queued'
            RETURNING kind, payload
            """,
            job_id,
        )
        if row is not None:
            await notify_job_update(conn, job_id)
    if row is None:
        logger.debug("job %s already picked up or not found — skipping", job_id)
        return

    kind: str = row["kind"]
    payload: dict[str, Any] = row["payload"] or {}
    logger.info("job %s starting kind=%s", job_id, kind)

    handler = _HANDLERS.get(kind)
    if handler is None:
        await _finish(
            pool,
            job_id,
            status="failed",
            error={"message": f"no handler registered for job kind {kind!r}"},
        )
        msg = f"no handler registered for job kind {kind!r}"
        logger.error("job %s failed: %s", job_id, msg)
        raise ValueError(msg)

    ctx = JobContext(job_id=job_id, _pool=pool)
    try:
        result = await handler(pool, http_client, payload, ctx)
        await _finish(pool, job_id, status="succeeded", result=result)
        logger.info("job %s succeeded", job_id)
    except asyncio.CancelledError:
        await _finish(pool, job_id, status="cancelled")
        logger.info("job %s cancelled", job_id)
        raise  # Re-raise to preserve task cancellation semantics
    except JobError as exc:
        error: dict[str, Any] = {"message": str(exc)}
        if exc.action_link is not None:
            error["action_link"] = exc.action_link
        await _finish(pool, job_id, status="failed", error=error)
        logger.warning("job %s failed (JobError): %s", job_id, exc)
    except Exception as exc:
        await _finish(
            pool,
            job_id,
            status="failed",
            error={"message": _sanitize_error_message(str(exc))},
        )
        logger.exception("job %s failed with unhandled exception", job_id)


# ---------------------------------------------------------------------------
# Stale-job reaper
# ---------------------------------------------------------------------------

_STALE_REAP_INTERVAL_SEC: float = 60.0


async def _reap_stale_jobs(pool: asyncpg.Pool, kinds: list[str]) -> int:
    """Mark stale ``running`` jobs owned by this worker as ``failed``.

    A job is considered stale if it has not emitted a heartbeat (via
    ``update_progress``) for 30 minutes.  ``COALESCE(last_heartbeat_at,
    started_at)`` is used so pre-migration rows with a NULL heartbeat still fall
    back to ``started_at`` for the staleness check.

    The ``kinds`` filter ensures each service only reaps jobs it owns — preventing
    paper_ingestion from killing pulse_ingestion jobs still running elsewhere.

    Args:
        pool:  asyncpg connection pool.
        kinds: job kinds this worker handles.

    Returns:
        Number of jobs reaped.
    """
    # Pass a dict rather than a JSON string so the asyncpg JSONB codec
    # (registered via ``init_pg_connection``) handles encoding.  Feeding a
    # pre-serialised string results in double-encoded values that later
    # decode as strings instead of objects.
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "UPDATE jobs"
            " SET status = 'failed',"
            "     error = $1::jsonb,"
            "     finished_at = NOW()"
            " WHERE status = 'running'"
            "   AND ($2::text[] = '{}' OR kind = ANY($2::text[]))"
            "   AND COALESCE(last_heartbeat_at, started_at)"
            "       < NOW() - INTERVAL '30 minutes'"
            " RETURNING id",
            {"message": "reaped as stale (no heartbeat)", "type": "StaleJobReaped"},
            kinds,
        )
        for row in rows:
            await notify_job_update(conn, str(row["id"]))
    count = len(rows)
    if count:
        logger.warning("jobs.reaper reaped %d stale job(s) for kinds=%s", count, kinds)
    return count


# ---------------------------------------------------------------------------
# worker_loop
# ---------------------------------------------------------------------------


async def worker_loop(
    pool: asyncpg.Pool,
    http_client: httpx.AsyncClient,
    *,
    kinds: set[str] | None = None,
    poll_interval: float = 2.0,
    stop_event: asyncio.Event,
) -> None:
    """Poll for queued jobs and run them concurrently.

    Args:
        pool: asyncpg connection pool.
        http_client: shared httpx client passed to handlers.
        kinds: restrict polling to these job kinds; ``None`` = any kind.
        poll_interval: seconds between polls when the queue is empty.
        stop_event: set this to stop the loop gracefully.
    """
    # Local sentinel — 0.0 forces the reaper to fire on the first iteration.
    # Using a local variable prevents cross-test state leakage.
    last_reap_ts: float = 0.0

    logger.info("jobs.worker_loop started (kinds=%s)", kinds or "all")
    kind_list: list[str] = sorted(kinds) if kinds else []

    while not stop_event.is_set():
        try:
            # Reap stale running jobs at most once per 60 seconds.
            # Pass kind_list so each service only reaps jobs it owns — prevents
            # paper_ingestion from killing pulse_ingestion jobs still running on
            # another service's worker.
            now = time.monotonic()
            if now - last_reap_ts >= _STALE_REAP_INTERVAL_SEC:
                last_reap_ts = now
                try:
                    await _reap_stale_jobs(pool, kind_list)
                except Exception:
                    logger.warning(
                        "jobs.worker_loop stale-job reaper failed — continuing",
                        exc_info=True,
                    )

            if kind_list:
                rows = await pool.fetch(
                    "SELECT id::text FROM jobs"
                    " WHERE status = 'queued' AND cancel_requested = FALSE"
                    " AND kind = ANY($1::text[])"
                    " ORDER BY created_at LIMIT 5",
                    kind_list,
                )
            else:
                rows = await pool.fetch(
                    "SELECT id::text FROM jobs"
                    " WHERE status = 'queued' AND cancel_requested = FALSE"
                    " ORDER BY created_at LIMIT 5"
                )

            if rows:
                await asyncio.gather(
                    *(run_job(pool, http_client, r["id"]) for r in rows),
                    return_exceptions=True,
                )
            else:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=poll_interval)
                except TimeoutError:
                    pass  # expected: poll interval elapsed, no stop signal — continue loop
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("jobs.worker_loop unexpected error — continuing")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=poll_interval)
            except TimeoutError:
                pass  # expected: poll interval elapsed, no stop signal — continue loop

    logger.info("jobs.worker_loop stopped")


# ---------------------------------------------------------------------------
# Test-only noop handler — only registered when JARVIS_ENABLE_TEST_JOBS=1 so
# that production workers cannot accidentally claim/execute it.
# ---------------------------------------------------------------------------


if os.environ.get("JARVIS_ENABLE_TEST_JOBS") == "1":

    @job_handler("noop.test")
    async def _noop_test(  # noqa: RUF029 — registered via @job_handler
        _pool: asyncpg.Pool,
        _http_client: httpx.AsyncClient,
        payload: dict[str, Any],
        ctx: JobContext,
    ) -> dict[str, Any]:
        await ctx.update_progress(0.5, "halfway")
        await asyncio.sleep(0.1)
        return {"ok": True, "echo": payload}
