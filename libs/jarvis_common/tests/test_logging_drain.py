"""TDD tests for SystemEventHandler drain bugs.

JC-BUG-01: aclose() doesn't flush pending events (cancel-only).
JC-BUG-02: _dropped not incremented on a failed batch (outage undercount).
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from jarvis_common.logging_config import (
    BoundedUDPLogHandler,
    ForwardingJSONFormatter,
    SystemEventHandler,
    configure_logging,
)
from jarvis_common.testing import make_pool_and_conn


def _make_mock_pool(
    *,
    executemany_side_effect: Any = None,
) -> tuple[MagicMock, AsyncMock]:
    """Build a shared pool with configurable batch-write failure."""
    pool, conn = make_pool_and_conn()
    conn.executemany = AsyncMock(side_effect=executemany_side_effect)
    conn.execute = AsyncMock()
    return pool, conn


def test_udp_forwarder_drops_immediately_when_queue_is_saturated() -> None:
    """A saturated optional destination never delays normal log emission."""
    handler = BoundedUDPLogHandler("localhost:9000", queue_size=1)
    handler.setFormatter(ForwardingJSONFormatter("test"))
    handler._stop.set()
    handler._thread.join(timeout=0.2)
    handler._queue.put_nowait(b"already full")
    record = logging.LogRecord("test", logging.INFO, "", 0, "message", (), None)

    handler.emit(record)

    assert handler._queue.qsize() == 1
    handler.close()


def test_logging_profile_off_has_no_udp_forwarder() -> None:
    """An empty optional address keeps structured stdout entirely local."""
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    try:
        configure_logging("test", log_forward_address="")
        assert not any(isinstance(handler, BoundedUDPLogHandler) for handler in root.handlers)
    finally:
        root.handlers.clear()
        root.handlers.extend(original_handlers)


# ---------------------------------------------------------------------------
# JC-BUG-02: _dropped not incremented when executemany raises
# ---------------------------------------------------------------------------


async def test_dropped_increments_on_failed_batch() -> None:
    """JC-BUG-02 RED→GREEN: outage batch drop must increment self._dropped."""
    pool, conn = _make_mock_pool(executemany_side_effect=RuntimeError("DB down"))

    handler = SystemEventHandler(pool, ring_buffer_size=100, flush_interval_s=0.01)

    # Enqueue 3 WARNING events so we get a batch of 3
    for i in range(3):
        record = logging.LogRecord(
            name="test",
            level=logging.WARNING,
            pathname="",
            lineno=0,
            msg=f"event {i}",
            args=(),
            exc_info=None,
        )
        handler.emit(record)

    assert handler._queue.qsize() == 3, "All 3 events should be queued"

    # Start the drain loop and let it run one cycle
    loop = asyncio.get_running_loop()
    handler._task = loop.create_task(handler._drain_loop())

    # Give the drain loop time to run one cycle (flush_interval=0.01s + a buffer)
    await asyncio.sleep(0.05)

    # Cancel to stop further looping
    handler._task.cancel()
    try:
        await handler._task
    except (asyncio.CancelledError, Exception):
        pass

    # BUG-02: _dropped must equal len(batch) = 3 after the failed executemany
    assert handler._dropped == 3, (
        f"Expected _dropped=3 after 3 events lost to outage, got {handler._dropped}"
    )
    assert handler._was_in_outage is True


async def test_dropped_recovery_row_includes_correct_count() -> None:
    """After recovery, the synthetic row captures the correct dropped count.

    Cycle 1: executemany raises → 2 events lost, _dropped=2, _was_in_outage=True.
    Cycle 2: a new event is queued so the batch is non-empty → executemany
             succeeds → recovery row INSERT with "2" is emitted, _dropped reset.
    """
    call_count = 0

    async def executemany_flaky(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("DB down on first call")
        # Second call succeeds

    pool, conn = _make_mock_pool(executemany_side_effect=executemany_flaky)

    handler = SystemEventHandler(pool, ring_buffer_size=100, flush_interval_s=0.01)

    # Enqueue 2 events — these will be lost to the first failed executemany
    for i in range(2):
        record = logging.LogRecord(
            name="test",
            level=logging.WARNING,
            pathname="",
            lineno=0,
            msg=f"event {i}",
            args=(),
            exc_info=None,
        )
        handler.emit(record)

    loop = asyncio.get_running_loop()
    handler._task = loop.create_task(handler._drain_loop())

    # Let cycle 1 run and fail (drops the 2 events, sets _was_in_outage=True)
    await asyncio.sleep(0.03)

    # Enqueue a recovery-cycle event so cycle 2 has a non-empty batch
    # (the recovery row is only emitted when batch is non-empty — which is
    # by design: a new event arriving after recovery proves the DB is back up)
    recovery_record = logging.LogRecord(
        name="test",
        level=logging.WARNING,
        pathname="",
        lineno=0,
        msg="recovery event",
        args=(),
        exc_info=None,
    )
    handler.emit(recovery_record)

    # Let cycle 2 run and succeed (flushes recovery_event + emits recovery row)
    await asyncio.sleep(0.05)

    handler._task.cancel()
    try:
        await handler._task
    except (asyncio.CancelledError, Exception):
        pass

    # After recovery: _dropped should be reset to 0 and the recovery row should
    # report 2 dropped events
    assert handler._dropped == 0, "After recovery, _dropped should be reset"
    # Check that conn.execute was called with the exact recovery message.
    # Production code: conn.execute(SQL, "error", "error", "SystemEventHandler",
    #                               f"dropped {dropped_count} events during outage")
    # args[0]=SQL, args[1]="error", args[2]="error", args[3]="SystemEventHandler",
    # args[4]= the message string.
    recovery_calls = [c for c in conn.execute.call_args_list if "dropped" in str(c)]
    assert recovery_calls, "No recovery row was inserted"
    assert recovery_calls[0].args[4] == "dropped 2 events during outage", (
        f"Recovery row message mismatch: {recovery_calls[0].args[4]!r}"
    )


# ---------------------------------------------------------------------------
# JC-BUG-01: aclose() must flush pending events before stopping
# ---------------------------------------------------------------------------


async def test_aclose_flushes_pending_events() -> None:
    """JC-BUG-01 RED→GREEN: aclose() must flush queued events, not just cancel."""
    pool, conn = _make_mock_pool()

    handler = SystemEventHandler(pool, ring_buffer_size=100, flush_interval_s=60.0)
    # Long interval so the drain loop doesn't fire on its own

    # Enqueue 2 events BEFORE starting the task
    for i in range(2):
        record = logging.LogRecord(
            name="test",
            level=logging.WARNING,
            pathname="",
            lineno=0,
            msg=f"shutdown event {i}",
            args=(),
            exc_info=None,
        )
        handler.emit(record)

    assert handler._queue.qsize() == 2, "Both events should be queued pre-aclose"

    # emit() starts the task automatically once the loop is running
    # (the task is set up in emit); but let's also ensure we have a running loop
    # If emit didn't start a task (no running loop at emit time), start it now
    loop = asyncio.get_running_loop()
    if handler._task is None or handler._task.done():
        handler._task = loop.create_task(handler._drain_loop())

    # Now call aclose() — this should flush the 2 pending events gracefully
    await handler.aclose()

    # Task must be done (exited cleanly, not cancelled with CancelledError)
    assert handler._task is not None
    assert handler._task.done(), "Task should be done after aclose()"

    # The 2 events must have been flushed via executemany BEFORE the task exited
    assert conn.executemany.called, (
        "executemany must have been called to flush events during aclose()"
    )
    # Check that the 2 events were passed to executemany
    all_rows = []
    for c in conn.executemany.call_args_list:
        rows = c.args[1] if len(c.args) > 1 else c.kwargs.get("args", [])
        all_rows.extend(rows)
    assert len(all_rows) == 2, f"Expected 2 flushed event rows, got {len(all_rows)}: {all_rows}"
    # Queue should be empty
    assert handler._queue.empty(), "Queue should be empty after aclose()"


async def test_aclose_task_is_done_after_call() -> None:
    """aclose() must result in the background task being fully done."""
    pool, conn = _make_mock_pool()
    handler = SystemEventHandler(pool, ring_buffer_size=100, flush_interval_s=60.0)

    loop = asyncio.get_running_loop()
    handler._task = loop.create_task(handler._drain_loop())

    await handler.aclose()

    assert handler._task.done(), "Task must be done after aclose() returns"


async def test_aclose_no_task_is_noop() -> None:
    """aclose() when no task was ever started must return without error."""
    pool, _ = _make_mock_pool()
    handler = SystemEventHandler(pool, ring_buffer_size=100, flush_interval_s=0.01)
    # Don't start a task
    assert handler._task is None
    await handler.aclose()  # Must not raise


# ---------------------------------------------------------------------------
# Change-3 validation: aclose() must terminate even when pool.acquire() blocks
# ---------------------------------------------------------------------------


async def test_aclose_terminates_when_acquire_blocks() -> None:
    """aclose() must complete within a bounded timeout when pool.acquire() hangs forever.

    Pre-condition (why the OLD aclose would hang):
        The old implementation did ``await self._task`` with no timeout.  If the
        drain loop is stuck inside ``async with self._pool.acquire()`` — because the
        pool is exhausted at shutdown — that await would block indefinitely.

    Post-condition (new aclose):
        ``asyncio.wait_for(self._task, timeout=max(flush_interval*2, 5.0))``
        fires after at most 5 s (floor) and cancels the task, so aclose() returns.

    The test uses ``timeout=8.0`` for wait_for around aclose() itself as a
    test-level guard.  The aclose() internal timeout floor is 5.0 s, so 8 s
    gives ample margin.
    """
    # Build a pool whose acquire() blocks forever (simulates pool exhaustion).
    blocking_event = asyncio.Event()  # never set → acquire waits forever

    pool = MagicMock()

    @asynccontextmanager
    async def _blocking_acquire():
        await blocking_event.wait()  # hangs until the event is set (never)
        yield AsyncMock()  # unreachable

    pool.acquire = _blocking_acquire

    # Use a tiny flush_interval so aclose timeout = max(0.01*2, 5.0) = 5.0 s.
    handler = SystemEventHandler(pool, ring_buffer_size=100, flush_interval_s=0.01)

    # Enqueue one event so the drain loop proceeds to the acquire() call.
    record = logging.LogRecord(
        name="test",
        level=logging.WARNING,
        pathname="",
        lineno=0,
        msg="blocking event",
        args=(),
        exc_info=None,
    )
    handler.emit(record)

    # Start the drain task manually (emit may have started it; ensure it's running).
    loop = asyncio.get_running_loop()
    if handler._task is None or handler._task.done():
        handler._task = loop.create_task(handler._drain_loop())

    # Give the drain loop just enough time to enter _pool.acquire() before aclose().
    await asyncio.sleep(0.05)

    # aclose() MUST complete within 8 s (the internal timeout is 5 s).
    # With the OLD aclose (no wait_for), this would hang forever — test would fail
    # with asyncio.TimeoutError from wait_for here.
    await asyncio.wait_for(handler.aclose(), timeout=8.0)

    # Drain task must be done after aclose() returns.
    assert handler._task is not None
    assert handler._task.done(), "Drain task must be done after aclose() returns"


async def test_aclose_counts_dropped_on_timeout_cancel() -> None:
    """aclose() timeout-cancel must count the events still queued as dropped.

    Pre-condition (bug): the ``except TimeoutError`` branch cancels the blocked
    drain task but the events still sitting in the queue are silently lost —
    ``_dropped`` is never incremented, so the recovery row under-reports.

    Post-condition (fix): on timeout, ``_dropped`` increases by ``qsize()``
    (the events still queued behind the blocked drain).
    """
    # Pool whose acquire() blocks forever → once the drain loop pulls its first
    # batch and reaches acquire() it never returns; any events emitted afterwards
    # pile up in the queue and are lost when aclose() times out and cancels.
    blocking_event = asyncio.Event()  # never set

    pool = MagicMock()

    @asynccontextmanager
    async def _blocking_acquire():
        await blocking_event.wait()
        yield AsyncMock()  # unreachable

    pool.acquire = _blocking_acquire

    handler = SystemEventHandler(pool, ring_buffer_size=100, flush_interval_s=0.01)

    # One event triggers the first batch → drain enters the blocking acquire().
    handler.emit(
        logging.LogRecord(
            name="test",
            level=logging.WARNING,
            pathname="",
            lineno=0,
            msg="batch trigger",
            args=(),
            exc_info=None,
        )
    )

    loop = asyncio.get_running_loop()
    if handler._task is None or handler._task.done():
        handler._task = loop.create_task(handler._drain_loop())

    # Let the drain loop pull that event into its batch and block on acquire().
    await asyncio.sleep(0.05)

    # Now emit 3 MORE events — these stay in the queue behind the blocked drain.
    for i in range(3):
        handler.emit(
            logging.LogRecord(
                name="test",
                level=logging.WARNING,
                pathname="",
                lineno=0,
                msg=f"stranded event {i}",
                args=(),
                exc_info=None,
            )
        )

    dropped_before = handler._dropped
    queued_at_timeout = handler._queue.qsize()
    assert queued_at_timeout == 3, (
        f"3 events should be stranded in the queue, got {queued_at_timeout}"
    )

    await asyncio.wait_for(handler.aclose(), timeout=8.0)

    assert handler._task is not None
    assert handler._task.done(), "Drain task must be done after aclose() returns"

    # The events still queued when aclose timed out must be counted as dropped.
    assert handler._dropped == dropped_before + queued_at_timeout, (
        f"aclose timeout-cancel must add the {queued_at_timeout} still-queued "
        f"events to _dropped (was {dropped_before}, got {handler._dropped})"
    )
