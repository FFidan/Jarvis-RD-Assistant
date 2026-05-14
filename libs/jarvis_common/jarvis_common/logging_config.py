"""Structured JSON logging for JARVIS services."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import uuid
from collections.abc import Callable
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    import asyncpg

request_id_ctx: ContextVar[str] = ContextVar("request_id", default="")

# Per-request/command correlation id.  Set by middleware (HTTP or Telegram)
# before dispatching to business logic; automatically picked up by log_event.
correlation_id_var: ContextVar[uuid.UUID | None] = ContextVar("correlation_id", default=None)


class JSONFormatter(logging.Formatter):
    """Format log records as single-line JSON.

    Parameters
    ----------
    service_name : str
        Name included in every log entry for service identification.
    """

    def __init__(self, service_name: str) -> None:
        super().__init__()
        self.service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        corr = correlation_id_var.get()
        log_entry = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service": self.service_name,
            "request_id": request_id_ctx.get(""),
            "correlation_id": str(corr) if corr else None,
        }
        if record.exc_info and record.exc_info[0] is not None:
            log_entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_entry)


class SystemEventHandler(logging.Handler):
    """Persists WARNING+ log records into the `system_events` table.

    Non-blocking: ``emit()`` enqueues a serialized event onto an
    ``asyncio.Queue``; a background task drains in batches. On overflow,
    drops oldest records and tracks a counter. On Postgres outage, falls
    back to ``sys.stderr`` so events are not silently lost; on recovery,
    emits one synthetic ``"dropped N events during outage"`` row.
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        *,
        ring_buffer_size: int = 1000,
        flush_interval_s: float = 0.5,
    ) -> None:
        super().__init__()
        self.setLevel(logging.WARNING)
        self._pool = pool
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=ring_buffer_size)
        self._flush_interval = flush_interval_s
        self._task: asyncio.Task | None = None
        self._dropped = 0
        self._was_in_outage = False

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno < self.level:
            return
        try:
            event = {
                "level": record.levelname.lower(),
                "category": getattr(record, "category", "error"),
                "source": record.name,
                "message": self.format(record) if self.formatter else record.getMessage(),
                "context": getattr(record, "context", {}),
                "correlation_id": correlation_id_var.get(),
            }
        except Exception:
            return
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self._dropped += 1
            try:
                self._queue.put_nowait(event)
            except asyncio.QueueFull:
                pass
        if self._task is None or self._task.done():
            try:
                loop = asyncio.get_running_loop()
                self._task = loop.create_task(self._drain_loop())
            except RuntimeError:
                pass

    async def _drain_loop(self) -> None:
        while True:
            await asyncio.sleep(self._flush_interval)
            batch: list[dict] = []
            while not self._queue.empty() and len(batch) < 100:
                try:
                    batch.append(self._queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            if not batch:
                continue
            try:
                async with self._pool.acquire() as conn:
                    await conn.executemany(
                        "INSERT INTO system_events "
                        "(level, category, source, message, context, correlation_id) "
                        "VALUES ($1, $2, $3, $4, $5::jsonb, $6)",
                        [
                            (
                                e["level"],
                                e["category"],
                                e["source"],
                                e["message"][:65535],
                                e["context"],
                                e["correlation_id"],
                            )
                            for e in batch
                        ],
                    )
                    if self._was_in_outage and self._dropped > 0:
                        # L-06: reset outage flags BEFORE the recovery INSERT so a
                        # failing INSERT cannot re-trigger the recovery branch on
                        # every subsequent drain (infinite emit loop). Snapshot
                        # the dropped count for the row payload first.
                        dropped_count = self._dropped
                        self._dropped = 0
                        self._was_in_outage = False
                        await conn.execute(
                            "INSERT INTO system_events (level, category, source, message) "
                            "VALUES ($1, $2, $3, $4)",
                            "error",
                            "error",
                            "SystemEventHandler",
                            f"dropped {dropped_count} events during outage",
                        )
            except Exception:
                self._was_in_outage = True
                for e in batch:
                    sys.stderr.write(f"[SystemEventHandler outage] {e}\n")

    async def aclose(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass


# L-07: keys carrying user-identifying or credential material that must never
# land in structured logs. The scrubber processor drops these keys from the
# event_dict before the renderer/formatter runs.
DEFAULT_PII_KEYS: frozenset[str] = frozenset(
    {
        "email",
        "token",
        "body",
        "api_key",
        "password",
        "secret",
        "Authorization",
        "X-API-Key",
    }
)


def _make_pii_scrubber(
    pii_keys: frozenset[str],
) -> Callable[[Any, str, dict[str, Any]], dict[str, Any]]:
    """Build a structlog processor that drops *pii_keys* from the event dict.

    The returned callable mutates ``event_dict`` in place and returns it
    (structlog processor contract). Keys absent from the event are ignored.
    """

    def _drop_pii_keys(
        _logger: Any, _method_name: str, event_dict: dict[str, Any]
    ) -> dict[str, Any]:
        for key in pii_keys:
            event_dict.pop(key, None)
        return event_dict

    return _drop_pii_keys


def configure_logging(
    service_name: str,
    log_level: str = "INFO",
    *,
    pii_keys: frozenset[str] | None = None,
) -> None:
    """Replace default logging config with structured JSON output.

    Wires both stdlib logging (via ``JSONFormatter``) and structlog so that
    code using either ``logging.getLogger`` or ``structlog.get_logger`` emits
    the same JSON shape.  structlog is configured to delegate to stdlib so the
    ``JSONFormatter`` remains the single authoritative renderer.

    Parameters
    ----------
    service_name : str
        Service identifier included in every log line.
    log_level : str
        Root logger level (default ``"INFO"``).
    pii_keys : frozenset[str] | None
        Keys to scrub from structlog event dicts before rendering (L-07).
        ``None`` uses :data:`DEFAULT_PII_KEYS`; pass an explicit set to extend
        or override (most callers should accept the default).
    """
    keys = pii_keys if pii_keys is not None else DEFAULT_PII_KEYS

    root = logging.getLogger()
    root.setLevel(getattr(logging, log_level.upper(), logging.INFO))
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter(service_name))
    root.addHandler(handler)
    # Quiet noisy third-party loggers
    for noisy in ("httpx", "httpcore", "asyncio", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # Wire structlog to emit through stdlib so JSONFormatter stays the single
    # renderer.  Processors up to PrintLoggerFactory are stdlib-agnostic;
    # PrintLoggerFactory is replaced by stdlib binding so events flow through
    # the handler + formatter wired above. The PII scrubber sits last (just
    # before wrap_for_formatter) so every other processor has already merged
    # its keys into the event_dict — anything sensitive added by upstream
    # processors gets dropped here.
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            _make_pii_scrubber(keys),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
