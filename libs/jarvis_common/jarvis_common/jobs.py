"""Async job queue backbone shared by all JARVIS microservices.

Provides:
- ``ProgressContext`` — Protocol for handler execution context.
- ``ProcrastinateJobContextShim`` — concrete adapter (re-exported from ``job_context``).
- ``JobError`` for structured errors with an optional action_link payload.
- ``JobLookupUnavailable`` for an infrastructure-caused lookup failure (503),
  distinct from a job that genuinely does not exist (404/None).
- ``get``, ``list_jobs`` — DB helpers.
- ``get_unified``, ``get_procrastinate_job_for_jarvis_id`` — procrastinate bridge.
- ``stream_job_events``, ``job_sse_payload`` — SSE streaming.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from typing import Any, Literal, Protocol, runtime_checkable

import asyncpg
import asyncpg_listen

from jarvis_common.job_context import ProcrastinateJobContextShim  # noqa: F401 — re-exported
from jarvis_common.sse import sse_event, sse_keepalive

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Job-handler ownership map (procrastinate queue routing)
# ---------------------------------------------------------------------------
#
# Maps each job kind to the procrastinate queue (= service name) that handles it.
# Single source of truth for queue routing: cross-service ``defer`` targets AND
# each service's worker consume-queue (in ``register_*_tasks``) resolve from this
# map via ``queue_for_kind``, so a queue rename changes exactly one place.
# ``test_queue_assignments_match_owner_map`` binds the real registration back to
# this map; ``register_tasks`` itself takes ``queue`` as an explicit arg.
#
# Ownership map:
JOB_HANDLER_OWNER: dict[str, Literal["paper_ingestion", "learning_engine", "telegram_bot"]] = {
    # paper_ingestion handlers
    "paper.process": "paper_ingestion",
    "paper.analyze": "paper_ingestion",
    "papers.batch_process": "paper_ingestion",
    "papers.batch_summarize": "paper_ingestion",
    "papers.process_library": "paper_ingestion",
    "papers.scan_local": "paper_ingestion",
    "paper.summarize": "paper_ingestion",
    "citations.batch_fetch": "paper_ingestion",
    "digest.weekly": "paper_ingestion",
    "extraction.single": "paper_ingestion",
    "extraction.batch": "paper_ingestion",
    "contradictions.scan": "paper_ingestion",
    "pulse.generate": "paper_ingestion",
    "pulse.train_classifier": "paper_ingestion",
    "model.pull": "paper_ingestion",
    "zotero.push": "paper_ingestion",
    "zotero.resync": "paper_ingestion",
    "zotero.sync_from_zotero": "paper_ingestion",
    "zotero.sync_annotations": "paper_ingestion",
    "zotero.push_highlights": "paper_ingestion",
    # learning_engine handlers
    "card.generate": "learning_engine",
    "card.generate_batch": "learning_engine",
}


def queue_for_kind(kind: str) -> str:
    """Return the procrastinate queue (owning service name) that handles *kind*.

    Use at cross-service enqueue sites and in each service's ``register_*_tasks``
    instead of a bare queue literal so a queue rename propagates from
    ``JOB_HANDLER_OWNER`` alone. Raises ``KeyError`` for an unknown kind
    (fail-fast: a typo must not silently route to a never-consumed queue).
    """
    return JOB_HANDLER_OWNER[kind]


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class ProgressContext(Protocol):
    """Full interface a job handler receives as its execution context.

    Concrete implementation: :class:`jarvis_common.job_context.ProcrastinateJobContextShim`.
    """

    job_id: str

    async def update_progress(self, progress: float, message: str | None = None) -> None:
        """Persist the current progress fraction (0.0–1.0) and optional status message."""
        ...

    async def is_cancelled(self) -> bool:
        """Return True when the job has been requested to abort."""
        ...

    async def record_terminal_outcome(
        self,
        *,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
        is_error: bool = False,
    ) -> bool:
        """Persist the job's terminal result or error payload.

        Declared here because the task wrapper calls it on every context it
        receives, so a context that omits it must fail this protocol rather
        than at run time. Returns False when a write was attempted and failed.
        """
        ...


# SSE keepalive / max-stream constants shared across routers
KEEPALIVE_INTERVAL = 15.0  # seconds between keepalive comments
MAX_STREAM_SECONDS = 750  # hard ceiling; yields streaming_timeout and exits
JOB_NOTIFY_CHANNEL = "jarvis_jobs"
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})
BatchTerminalStatus = Literal["ok", "partial", "cancelled"]


def batch_terminal_status(*, cancelled: bool, incomplete: bool) -> BatchTerminalStatus:
    """Return the shared terminal outcome for a batch handler.

    Cancellation has priority because the caller stopped the run even when
    failures also occurred. Otherwise any failed, blocked, skipped, or
    unprocessed work makes the result partial.
    """
    if cancelled:
        return "cancelled"
    if incomplete:
        return "partial"
    return "ok"


# B.4 Step 2: procrastinate broker bridge.
# The procrastinate schema (migration 052) emits NOTIFY on this channel for
# job inserts and abort_requested events. There is NO procrastinate trigger
# for status transitions (todo→doing→succeeded/failed) — those are observed
# via polling the procrastinate_jobs table on each stream cycle.
PROCRASTINATE_NOTIFY_CHANNEL = "procrastinate_any_queue_v1"

# Map procrastinate's status enum → JARVIS legacy status strings.
# Source: db/migrations/052_procrastinate_schema.sql:27-35.
#
# ``aborting`` maps to ``running``, NOT to a terminal status: procrastinate sets
# it the moment cancellation is *requested* while the handler is still executing.
# It is a request, not an outcome. Mapping it to ``cancelled`` made it terminal
# (see ``TERMINAL_STATUSES``), which closed the SSE stream with ``result: null``
# and discarded the handler's real final row — e.g. a ``papers.process_library``
# run that stopped early still returns its accumulated counts under a
# ``status: "cancelled"`` result dict, and the client never saw it.
# The request itself is not lost: ``procrastinate_row_to_jarvis_row`` still
# raises ``cancel_requested`` for ``aborting``. Only the genuine outcomes
# ``cancelled`` (aborted before it ever ran) and ``aborted`` (handler
# acknowledged the abort) are terminal.
PROCRASTINATE_STATUS_MAP: dict[str, str] = {
    "todo": "queued",
    "doing": "running",
    "succeeded": "succeeded",
    "failed": "failed",
    "cancelled": "cancelled",
    "aborting": "running",
    "aborted": "cancelled",
}

# Pre-built SQL CASE fragment derived from PROCRASTINATE_STATUS_MAP.
# Used in _list_jobs queries to avoid hand-duplicating the mapping.
# PROCRASTINATE_STATUS_MAP is a trusted module-level constant (no user input),
# so string interpolation here is safe.
_STATUS_CASE_SQL = (
    "CASE pj.status "
    + " ".join(f"WHEN '{k}' THEN '{v}'" for k, v in PROCRASTINATE_STATUS_MAP.items())
    + " ELSE 'running' END"
)


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


def job_sse_payload(row: dict[str, Any]) -> dict[str, Any]:
    """Return the public SSE payload for a procrastinate job row.

    ``cancel_requested`` is published alongside ``status`` because the two are
    orthogonal: a job whose abort was requested keeps reporting ``running``
    until the handler actually stops (see ``PROCRASTINATE_STATUS_MAP``), so the
    flag is the only signal that lets the UI show a "Cancelling" state rather
    than an indicator that looks untouched.
    """
    event_data: dict[str, Any] = {
        "progress": row.get("progress"),
        "progress_message": row.get("progress_message"),
        "status": row["status"],
        "cancel_requested": bool(row.get("cancel_requested")),
        "source": "procrastinate",
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
    """Adapt a ``procrastinate_jobs`` row into the full legacy ``jobs``-row shape.

    Returns a dict matching the legacy Job interface (12+ keys) with an
    additional ``source`` discriminator set to ``"procrastinate"``.  The
    ``cancel_requested`` flag is synthesised from the procrastinate status so
    callers don't need to special-case it. It is orthogonal to ``status``: an
    ``aborting`` row reports ``status="running"`` (the handler has not stopped
    yet) together with ``cancel_requested=True``.

    The extra keys (``id``, ``kind``, ``user_id``, ``created_at``, etc.) are
    required by ``get_unified`` so that route handlers can call
    ``_owner_matches``, ``serialise_row``, and the cancel branch without
    additional per-field checks.

    ``progress`` and ``progress_message`` are lifted from the LEFT-JOINed
    ``job_progress`` row (migration 054) when present; absent rows degrade
    to ``progress=0`` / ``progress_message=None``.
    """
    args: dict[str, Any] = prow.get("args") or {}
    status = procrastinate_status_to_jarvis(prow["status"])
    cancel_requested = prow["status"] in {"cancelled", "aborting", "aborted"}
    progress = prow.get("progress")
    if progress is None:
        progress = 0
    return {
        "id": args.get("job_id"),
        "kind": prow["task_name"],
        "user_id": args.get("user_id"),
        "status": status,
        "progress": progress,
        "progress_message": prow.get("progress_message"),
        "payload": {k: v for k, v in args.items() if k not in {"job_id", "user_id"}},
        "result": prow.get("result"),
        "error": prow.get("error"),
        "created_at": prow.get("created_at"),
        "started_at": prow.get("started_at"),
        "finished_at": prow.get("finished_at"),
        "cancel_requested": cancel_requested,
        "source": "procrastinate",
    }


async def get_procrastinate_job_for_jarvis_id(
    pool: asyncpg.Pool, jarvis_job_id: str
) -> dict[str, Any] | None:
    """Fetch the matching ``procrastinate_jobs`` row by ``args->>'job_id'``.

    Returns ``None`` when:
      * no procrastinate row carries this JARVIS job_id, or
      * the ``procrastinate_jobs`` table does not exist (migration 052 not
        applied — graceful degradation so legacy-only DBs still work).

    The SELECT LEFT-JOINs ``job_progress`` (migration 054) so callers can
    surface the latest progress snapshot through the SSE bridge. When
    migration 054 has not been applied (older DBs), the JOIN is silently
    dropped and the result still contains the procrastinate columns with
    ``progress`` / ``progress_message`` as ``None``.

    Raises :class:`JobLookupUnavailable` for any other lookup failure (e.g. a
    DB outage) — an infrastructure failure must not be reported the same way
    as "no such job".
    """
    sql_with_progress = """
        SELECT
          pj.id, pj.queue_name, pj.task_name, pj.status, pj.args, pj.attempts,
          jp.progress AS progress,
          jp.message  AS progress_message,
          jp.result   AS result,
          jp.error    AS error,
          (SELECT MIN(at) FROM procrastinate_events WHERE job_id = pj.id) AS created_at,
          (SELECT MIN(at) FROM procrastinate_events WHERE job_id = pj.id AND type = 'started') AS started_at,
          (SELECT MAX(at) FROM procrastinate_events WHERE job_id = pj.id AND type IN ('succeeded','failed','cancelled','aborted')) AS finished_at
        FROM procrastinate_jobs pj
        LEFT JOIN job_progress jp ON jp.jarvis_job_id = pj.args->>'job_id'
        WHERE pj.args->>'job_id' = $1
        ORDER BY pj.id DESC
        LIMIT 1
    """
    sql_without_progress = """
        SELECT
          pj.id, pj.queue_name, pj.task_name, pj.status, pj.args, pj.attempts,
          NULL::REAL AS progress,
          NULL::TEXT AS progress_message,
          NULL::jsonb AS result,
          NULL::jsonb AS error,
          (SELECT MIN(at) FROM procrastinate_events WHERE job_id = pj.id) AS created_at,
          (SELECT MIN(at) FROM procrastinate_events WHERE job_id = pj.id AND type = 'started') AS started_at,
          (SELECT MAX(at) FROM procrastinate_events WHERE job_id = pj.id AND type IN ('succeeded','failed','cancelled','aborted')) AS finished_at
        FROM procrastinate_jobs pj
        WHERE pj.args->>'job_id' = $1
        ORDER BY pj.id DESC
        LIMIT 1
    """
    try:
        async with pool.acquire() as conn:
            try:
                row = await conn.fetchrow(sql_with_progress, str(jarvis_job_id))
            except (asyncpg.UndefinedColumnError, asyncpg.UndefinedTableError):
                # job_progress missing (migration 054 not applied) — retry
                # or terminal-outcome columns missing (migration 058 not applied)
                # — retry without the JOIN so callers still see procrastinate state.
                row = await conn.fetchrow(sql_without_progress, str(jarvis_job_id))
    except asyncpg.UndefinedTableError:
        # procrastinate_jobs missing (migration 052 not applied).
        return None
    except Exception as exc:
        logger.warning("procrastinate row lookup failed for job %s", jarvis_job_id, exc_info=True)
        raise JobLookupUnavailable(f"job lookup failed for {jarvis_job_id}") from exc
    if row is None:
        return None
    return dict(row)


async def get_unified(pool: asyncpg.Pool, job_id: str) -> dict[str, Any] | None:
    """Lookup a job exclusively from the procrastinate table.

    Returns a row dict in the legacy Job interface shape, or None if not found.
    The ``source`` field is always ``"procrastinate"``. Propagates
    :class:`JobLookupUnavailable` from :func:`get_procrastinate_job_for_jarvis_id`
    on an infrastructure lookup failure.
    """
    prow = await get_procrastinate_job_for_jarvis_id(pool, job_id)
    if prow is None:
        return None
    return procrastinate_row_to_jarvis_row(prow)


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

    Polls the ``procrastinate_jobs`` table on each cycle and normalises the row
    to the legacy SSE payload shape via :func:`procrastinate_row_to_jarvis_row`.
    The stream terminates when the job reaches a JARVIS-terminal status
    (``succeeded`` / ``failed`` / ``cancelled``).

    A cancellation *request* (procrastinate ``aborting``) is deliberately NOT
    terminal — see ``PROCRASTINATE_STATUS_MAP`` — so the stream stays open until
    the handler actually stops and its final row, result included, is emitted.
    A worker that dies mid-abort cannot hang the stream forever: the
    ``MAX_STREAM_SECONDS`` ceiling still yields ``streaming_timeout`` and exits.

    A lookup failure caused by an infrastructure outage
    (:class:`JobLookupUnavailable`) is distinct from the job genuinely not
    existing: it yields a single ``{"error": "status_unavailable"}`` frame and
    then closes the stream, rather than the silent break used when the
    procrastinate row is simply absent.
    """
    # The change-detection key carries ``cancel_requested`` alongside progress and
    # status because a cancel request no longer moves ``status`` (doing → aborting
    # both map to "running"); without it the request would never be re-emitted and
    # the UI could not tell "Cancelling" from an untouched run.
    last_procrastinate_key: tuple[Any, Any, Any, bool] | None = None
    loop = asyncio.get_running_loop()
    loop_start = loop.time()
    last_keepalive = loop_start

    poll_interval = 2.0
    idle_start: float = loop.time()
    last_state: tuple[Any, Any, Any, bool] | None = None

    while True:
        if await is_disconnected():
            logger.debug("SSE client disconnected for job %s", job_id)
            break

        now = loop.time()
        elapsed = now - loop_start

        if elapsed > MAX_STREAM_SECONDS:
            logger.warning("SSE stream timeout for job %s after %.0fs", job_id, elapsed)
            yield sse_event({"status": "streaming_timeout"})
            break

        if now - last_keepalive >= KEEPALIVE_INTERVAL:
            yield sse_keepalive()
            last_keepalive = now

        try:
            procrastinate_raw = await get_procrastinate_job_for_jarvis_id(pool, job_id)
        except JobLookupUnavailable:
            yield sse_event({"error": "status_unavailable"})
            break

        # Emit procrastinate frame if changed.
        procrastinate_row: dict[str, Any] | None = None
        if procrastinate_raw is not None:
            procrastinate_row = procrastinate_row_to_jarvis_row(procrastinate_raw)
            procrastinate_key = (
                procrastinate_row.get("progress"),
                procrastinate_row.get("progress_message"),
                procrastinate_row["status"],
                bool(procrastinate_row.get("cancel_requested")),
            )
            if procrastinate_key != last_procrastinate_key:
                last_procrastinate_key = procrastinate_key
                yield sse_event(job_sse_payload(procrastinate_row))

        # If no procrastinate row exists, the job is unknown — terminate.
        if procrastinate_row is None:
            break

        # Adaptive poll throttling.
        current_state = (
            procrastinate_row.get("progress"),
            procrastinate_row.get("progress_message"),
            procrastinate_row["status"],
            bool(procrastinate_row.get("cancel_requested")),
        )
        if current_state != last_state:
            last_state = current_state
            idle_start = loop.time()
            poll_interval = 2.0
        else:
            if loop.time() - idle_start > 30:
                idle_start = loop.time()
                poll_interval = min(poll_interval + 1.0, 5.0)

        if procrastinate_row["status"] in TERMINAL_STATUSES:
            break

        await _wait_for_job_notification(pool, job_id, poll_interval)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class JobLookupUnavailable(RuntimeError):  # noqa: N818 -- distinct concept from JobError below
    """A job lookup failed for an infrastructure reason, not because the job is missing.

    Raised by :func:`get_procrastinate_job_for_jarvis_id` when the DB call fails
    for anything other than the two recognised schema-degradation cases
    (``job_progress``/``procrastinate_jobs`` not yet migrated, which legitimately
    degrade to ``None``). Callers must report this as 503, never fold it into
    the same "not found" (404) response used for a job that truly does not exist.
    """


class JobError(Exception):
    """Raise from a handler to produce a structured error with an optional link."""

    def __init__(self, message: str, *, action_link: dict[str, Any] | None = None) -> None:
        """Store the error message and an optional ``{"label": ..., "url": ...}`` action link."""
        super().__init__(message)
        self.action_link = action_link


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


async def list_jobs(
    pool: asyncpg.Pool,
    *,
    status: str | None = None,
    kind: str | None = None,
    limit: int = 50,
    user_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return jobs filtered by status and/or kind, newest first.

    Queries the ``procrastinate_jobs`` table exclusively (the legacy ``jobs``
    table has been dropped as of migration 053).  A ``source`` discriminator
    column with value ``"procrastinate"`` is added to every row for API
    compatibility.

    ``status="active"`` is the aggregate filter for queued and running jobs.
    When ``user_id`` is provided only jobs owned by that user are returned
    (rows where ``args->>'user_id' = user_id``).  Passing ``user_id=None``
    returns only system jobs — rows where ``args->>'user_id' IS NULL``.
    There is no "return all jobs" mode; to list jobs across all users, query
    the table directly or call this function once per known user.
    """
    # Fixed-position parameters (matches the $1/$2/$3/$4 placeholders in the query).
    # NULL params cause the corresponding WHERE clause to be skipped via IS NULL guard.
    params: list[Any] = [
        status,  # $1 — status filter or NULL
        kind,  # $2 — kind filter or NULL
        user_id,  # $3 — user_id filter or NULL (text)
        limit,  # $4 — LIMIT
    ]
    status_filter_sql = f"""
        (
          $1::text IS NULL
          OR ($1 = 'active' AND {_STATUS_CASE_SQL} IN ('queued', 'running'))
          OR ($1 <> 'active' AND {_STATUS_CASE_SQL} = $1)
        )
    """

    query_with_progress = f"""
        SELECT pj.args->>'job_id' AS id,
               pj.task_name AS kind,
               pj.args->>'user_id' AS user_id,
               {_STATUS_CASE_SQL} AS status,
               pj.args - 'job_id' - 'user_id' AS payload,
               jp.result AS result,
               jp.error AS error,
               COALESCE(jp.progress, 0)::float AS progress,
               jp.message AS progress_message,
               (SELECT MIN(at) FROM procrastinate_events WHERE job_id = pj.id) AS created_at,
               (SELECT MIN(at) FROM procrastinate_events WHERE job_id = pj.id AND type = 'started') AS started_at,
               (SELECT MAX(at) FROM procrastinate_events WHERE job_id = pj.id AND type IN ('succeeded','failed','cancelled','aborted')) AS finished_at,
               'procrastinate' AS source
        FROM procrastinate_jobs pj
        LEFT JOIN job_progress jp ON jp.jarvis_job_id = pj.args->>'job_id'
        WHERE pj.args ? 'job_id'
          AND {status_filter_sql}
          AND ($2::text IS NULL OR pj.task_name = $2)
          AND (
            ($3::text IS NULL AND pj.args->>'user_id' IS NULL)
            OR ($3::text IS NOT NULL AND pj.args->>'user_id' = $3)
          )
        ORDER BY (SELECT MIN(at) FROM procrastinate_events WHERE job_id = pj.id) DESC NULLS LAST
        LIMIT $4
    """
    query_without_progress = f"""
        SELECT pj.args->>'job_id' AS id,
               pj.task_name AS kind,
               pj.args->>'user_id' AS user_id,
               {_STATUS_CASE_SQL} AS status,
               pj.args - 'job_id' - 'user_id' AS payload,
               NULL::jsonb AS result,
               NULL::jsonb AS error,
               0::float AS progress,
               NULL::text AS progress_message,
               (SELECT MIN(at) FROM procrastinate_events WHERE job_id = pj.id) AS created_at,
               (SELECT MIN(at) FROM procrastinate_events WHERE job_id = pj.id AND type = 'started') AS started_at,
               (SELECT MAX(at) FROM procrastinate_events WHERE job_id = pj.id AND type IN ('succeeded','failed','cancelled','aborted')) AS finished_at,
               'procrastinate' AS source
        FROM procrastinate_jobs pj
        WHERE pj.args ? 'job_id'
          AND {status_filter_sql}
          AND ($2::text IS NULL OR pj.task_name = $2)
          AND (
            ($3::text IS NULL AND pj.args->>'user_id' IS NULL)
            OR ($3::text IS NOT NULL AND pj.args->>'user_id' = $3)
          )
        ORDER BY (SELECT MIN(at) FROM procrastinate_events WHERE job_id = pj.id) DESC NULLS LAST
        LIMIT $4
    """

    async with pool.acquire() as conn:
        try:
            rows = await conn.fetch(query_with_progress, *params)
        except (asyncpg.UndefinedColumnError, asyncpg.UndefinedTableError):
            rows = await conn.fetch(query_without_progress, *params)
    return [dict(r) for r in rows]
