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
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import asyncpg
import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

HandlerFn = Callable[
    [asyncpg.Pool, httpx.AsyncClient, dict[str, Any], "JobContext"],
    Awaitable[dict[str, Any]],
]

_HANDLERS: dict[str, HandlerFn] = {}

# Strip ANSI escape codes and absolute paths from error messages before persisting.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_PATH_RE = re.compile(r"(/[^\s]+)+")


def _sanitize_error_message(raw: str) -> str:
    msg = _ANSI_RE.sub("", raw)
    msg = _PATH_RE.sub("<path>", msg)
    return msg[:500]


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
        """Persist progress (0–1) and optional human-readable message to the DB."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE jobs SET progress = $1, progress_message = $2 WHERE id = $3::uuid",
                progress,
                message,
                self.job_id,
            )
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
    assert row is not None
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
    user_id: int | None = None,
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
    """Set cancel_requested=TRUE for the given job."""
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE jobs SET cancel_requested = TRUE WHERE id = $1::uuid",
            job_id,
        )


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
            error={"message": f"no handler for {kind}"},
        )
        logger.warning("job %s failed: no handler for kind %s", job_id, kind)
        return

    ctx = JobContext(job_id=job_id, _pool=pool)
    try:
        result = await handler(pool, http_client, payload, ctx)
        await _finish(pool, job_id, status="succeeded", result=result)
        logger.info("job %s succeeded", job_id)
    except asyncio.CancelledError:
        await _finish(pool, job_id, status="cancelled")
        logger.info("job %s cancelled", job_id)
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


async def _reap_stale_jobs(pool: asyncpg.Pool) -> None:
    """Mark any ``running`` job older than 30 minutes as ``failed``.

    Called periodically from ``worker_loop`` to clean up jobs that were
    abandoned when a worker crashed or restarted mid-handler.
    """
    # Pass a dict rather than a JSON string so the asyncpg JSONB codec
    # (registered via ``init_pg_connection``) handles encoding.  Feeding a
    # pre-serialised string results in double-encoded values that later
    # decode as strings instead of objects.
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE jobs SET status='failed',"
            " error = $1::jsonb,"
            " finished_at = NOW()"
            " WHERE status='running'"
            "   AND started_at < NOW() - INTERVAL '30 minutes'",
            {"message": "Job stalled: worker restart or crash"},
        )


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
            now = time.monotonic()
            if now - last_reap_ts >= _STALE_REAP_INTERVAL_SEC:
                last_reap_ts = now
                try:
                    await _reap_stale_jobs(pool)
                except Exception:
                    logger.exception("jobs.worker_loop stale-job reaper failed — continuing")

            if kind_list:
                rows = await pool.fetch(
                    "SELECT id::text FROM jobs WHERE status = 'queued' AND kind = ANY($1::text[])"
                    " ORDER BY created_at LIMIT 5",
                    kind_list,
                )
            else:
                rows = await pool.fetch(
                    "SELECT id::text FROM jobs WHERE status = 'queued' ORDER BY created_at LIMIT 5"
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
                    pass
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("jobs.worker_loop unexpected error — continuing")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=poll_interval)
            except TimeoutError:
                pass

    logger.info("jobs.worker_loop stopped")


# ---------------------------------------------------------------------------
# Dev-only noop handler — only registered when DEV_MODE is enabled so that
# production workers cannot accidentally claim/execute it.
# ---------------------------------------------------------------------------


if os.environ.get("DEV_MODE", "").lower() in ("1", "true", "yes"):

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
