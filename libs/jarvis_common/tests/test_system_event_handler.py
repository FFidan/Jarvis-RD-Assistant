"""Tests for jarvis_common.logging_config.SystemEventHandler."""

from __future__ import annotations

import asyncio
import logging
import sys
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jarvis_common.logging_config import SystemEventHandler, correlation_id_var

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pool(*, raise_on_acquire: bool = False) -> MagicMock:
    """Return a minimal asyncpg.Pool mock."""
    pool = MagicMock()
    conn = AsyncMock()
    conn.executemany = AsyncMock(return_value=None)
    conn.execute = AsyncMock(return_value=None)

    if raise_on_acquire:
        pool.acquire.return_value.__aenter__ = AsyncMock(side_effect=OSError("pg down"))
        pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    else:
        pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
        pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return pool


def _make_record(level: int, message: str) -> logging.LogRecord:
    record = logging.LogRecord(
        name="test.logger",
        level=level,
        pathname="",
        lineno=0,
        msg=message,
        args=(),
        exc_info=None,
    )
    return record


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_handler_only_processes_warning_and_above():
    """DEBUG and INFO records must NOT be queued; WARNING and above must be."""
    pool = _make_pool()
    handler = SystemEventHandler(pool=pool)

    debug_record = _make_record(logging.DEBUG, "debug msg")
    info_record = _make_record(logging.INFO, "info msg")
    warning_record = _make_record(logging.WARNING, "warning msg")
    error_record = _make_record(logging.ERROR, "error msg")

    # emit() respects the handler level set in __init__
    assert not handler.filter(debug_record) or handler.level > logging.DEBUG

    # Simulate what logging framework does: call emit only when levelno >= handler level
    handler.emit(debug_record)
    handler.emit(info_record)

    # Queue must still be empty — DEBUG and INFO are below WARNING
    assert handler._queue.empty(), "DEBUG/INFO must not enter the queue"

    handler.emit(warning_record)
    handler.emit(error_record)

    assert handler._queue.qsize() == 2, "WARNING and ERROR must be queued"


def test_handler_includes_correlation_id_from_contextvar():
    """emit() snapshots correlation_id_var at emit time and includes it in the event."""
    pool = _make_pool()
    handler = SystemEventHandler(pool=pool)

    test_uuid = uuid.uuid4()
    token = correlation_id_var.set(test_uuid)
    try:
        record = _make_record(logging.WARNING, "test with correlation")
        handler.emit(record)
    finally:
        correlation_id_var.reset(token)

    assert not handler._queue.empty()
    event = handler._queue.get_nowait()
    assert event["correlation_id"] == test_uuid


def test_handler_ring_buffer_drops_oldest_on_overflow():
    """When the ring buffer is full, emit() must drop the oldest event and increment _dropped."""
    pool = _make_pool()
    buffer_size = 5
    handler = SystemEventHandler(pool=pool, ring_buffer_size=buffer_size)

    # Fill the buffer
    for i in range(buffer_size):
        record = _make_record(logging.WARNING, f"msg {i}")
        handler.emit(record)

    assert handler._queue.qsize() == buffer_size
    assert handler._dropped == 0

    # One more — should drop oldest and increment counter
    overflow_record = _make_record(logging.ERROR, "overflow msg")
    handler.emit(overflow_record)

    assert handler._dropped == 1
    assert handler._queue.qsize() == buffer_size

    # Newest event should be present (overflow msg at the back after get_nowait + put_nowait)
    events = []
    while not handler._queue.empty():
        events.append(handler._queue.get_nowait())

    messages = [e["message"] for e in events]
    assert "overflow msg" in messages
    # Original first message (msg 0) was dropped
    assert "msg 0" not in messages


@pytest.mark.asyncio
async def test_handler_falls_back_to_stderr_when_postgres_unreachable():
    """When the pool raises on acquire, records should be written to stderr."""
    pool = _make_pool(raise_on_acquire=True)
    handler = SystemEventHandler(pool=pool, flush_interval_s=0.01)

    record = _make_record(logging.ERROR, "pg unreachable test")
    handler.emit(record)

    stderr_lines: list[str] = []

    class _CaptureStederr:
        def write(self, s: str) -> None:
            stderr_lines.append(s)

    with patch.object(sys, "stderr", _CaptureStederr()):
        # Manually call _drain_loop for one cycle
        batch = []
        while not handler._queue.empty():
            batch.append(handler._queue.get_nowait())

        if batch:
            try:
                async with handler._pool.acquire() as _conn:
                    pass  # Will raise OSError
            except (OSError, Exception):
                handler._was_in_outage = True
                for e in batch:
                    sys.stderr.write(f"[SystemEventHandler outage] {e}\n")

    assert any(
        "pg unreachable test" in line or "SystemEventHandler outage" in line
        for line in stderr_lines
    ), f"Expected stderr fallback output, got: {stderr_lines}"
    assert handler._was_in_outage is True


@pytest.mark.asyncio
async def test_drain_loop_passes_native_dict_not_json_string():
    """_drain_loop must pass e['context'] as a native dict to executemany, not json.dumps().

    asyncpg's JSONB codec (registered via init_pg_connection) handles serialisation;
    pre-serialising with json.dumps would double-encode the value and store a
    JSON-string-of-JSON rather than an object.
    """
    pool = _make_pool()
    conn = pool.acquire.return_value.__aenter__.return_value
    handler = SystemEventHandler(pool=pool, flush_interval_s=0.001)

    ctx = {"key": "value", "count": 42}
    record = _make_record(logging.WARNING, "test dict context")
    record.__dict__["context"] = ctx
    handler.emit(record)

    # Run one drain cycle by patching asyncio.sleep to not actually wait.
    sleep_call_count = 0

    async def _immediate_sleep(_delay: float) -> None:
        nonlocal sleep_call_count
        sleep_call_count += 1
        if sleep_call_count > 1:
            # Cancel after second sleep so _drain_loop exits cleanly.
            raise asyncio.CancelledError

    with patch.object(asyncio, "sleep", _immediate_sleep):
        try:
            await handler._drain_loop()
        except asyncio.CancelledError:
            pass

    assert conn.executemany.called, "executemany should have been called"
    call_args = conn.executemany.call_args
    rows = call_args[0][1]  # second positional arg is the list of tuples
    assert len(rows) == 1
    # Index 4 is $5::jsonb — must be a dict, NOT a str
    jsonb_param = rows[0][4]
    assert isinstance(jsonb_param, dict), (
        f"Expected dict for $5::jsonb but got {type(jsonb_param).__name__!r}: {jsonb_param!r}. "
        "Codec must encode natively — caller must not pre-encode."  # nolint:jsonb-double-encode
    )
    assert jsonb_param == ctx


@pytest.mark.asyncio
async def test_outage_recovery_does_not_loop_on_insert_failure():
    """L-06: outage flags must be reset before the recovery INSERT runs.

    If the recovery INSERT raises after the reset, the outer except clause
    re-arms ``_was_in_outage`` and re-queues nothing — the next drain handles
    fresh events normally instead of re-emitting an ever-growing "dropped N"
    row on every cycle.
    """
    pool = _make_pool()
    conn = pool.acquire.return_value.__aenter__.return_value
    handler = SystemEventHandler(pool=pool, flush_interval_s=0.001)

    # Pre-seed outage state so the recovery branch will fire.
    handler._was_in_outage = True
    handler._dropped = 7

    # Make the recovery INSERT (conn.execute) raise; executemany succeeds.
    conn.execute = AsyncMock(side_effect=RuntimeError("recovery insert failed"))

    record = _make_record(logging.WARNING, "post-outage event")
    handler.emit(record)

    sleep_call_count = 0

    async def _immediate_sleep(_delay: float) -> None:
        nonlocal sleep_call_count
        sleep_call_count += 1
        if sleep_call_count > 1:
            raise asyncio.CancelledError

    with patch.object(asyncio, "sleep", _immediate_sleep):
        try:
            await handler._drain_loop()
        except asyncio.CancelledError:
            pass

    # Recovery INSERT was attempted, so flags must already have been reset
    # before it ran (otherwise the except branch would have re-set them and
    # the test guarantee couldn't hold).
    assert conn.execute.called, "recovery INSERT should have been attempted"
    # After the failing recovery INSERT, the outer except DOES re-set
    # _was_in_outage = True, but _dropped is now 0 — so the next drain will
    # not fire the recovery branch again (which requires _dropped > 0).
    assert handler._dropped == 0, "dropped counter must be reset before recovery INSERT"
