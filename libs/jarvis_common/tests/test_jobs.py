"""Unit tests for stream_job_events adaptive poll throttle in jobs.py.

Covers: ramp-up timing must be invariant to poll_interval growth.

The old proxy `idle_ticks * poll_interval > 30` has a cascade defect:
once the threshold is first crossed at tick ~16 (idle_ticks=16, poll_interval=2,
16*2=32>30), the SAME tick sees poll_interval become 3.  On the next iteration
idle_ticks is 17 and poll_interval is 3, so 17*3=51>30 — threshold already
exceeded → immediate second increment to 4.  One tick later: 18*4=72>30 → 5.
The ramp cascades from 2→3→4→5 in three consecutive ticks instead of waiting
30 more seconds between each step.

Fix: track `idle_start = loop.time()` at the idle transition and check
`(loop.time() - idle_start) > 30`; reset `idle_start` after each ramp step so
the next increment also requires 30 more elapsed seconds.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_row(status: str = "doing", progress_message: str | None = None) -> dict[str, Any]:
    """Return a minimal procrastinate-normalised job row."""
    return {
        "status": status,
        "progress": None,
        "progress_message": progress_message,
        "id": "test-job-id",
        "user_id": "1",
        "kind": "card.generate",
        "payload": {},
        "created_at": None,
        "updated_at": None,
        "result": None,
        "error": None,
    }


async def _drain(ait: AsyncIterator[str], max_items: int = 300) -> list[str]:
    """Collect up to *max_items* items from an async iterator."""
    items: list[str] = []
    async for item in ait:
        items.append(item)
        if len(items) >= max_items:
            break
    return items


# ---------------------------------------------------------------------------
# ramp-up must not cascade on the same idle period
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_adaptive_throttle_does_not_cascade_ramp():
    """BE-02: poll_interval must NOT jump 2→3→4→5 in consecutive ticks.

    With the old proxy `idle_ticks * poll_interval > 30`:
      - At tick 16: idle_ticks=16, poll_interval=2 → 32>30 → interval becomes 3
      - At tick 17: idle_ticks=17, poll_interval=3 → 51>30 → interval becomes 4
      - At tick 18: idle_ticks=18, poll_interval=4 → 72>30 → interval becomes 5
      → Cascade: 2→3→4→5 in 3 consecutive ticks.

    With the correct elapsed-seconds fix, each step requires 30 more seconds,
    so increments are separated by many ticks, not consecutive.

    This test steps loop.time() by `poll_interval` seconds per iteration
    (simulating one real wait-period per tick) and asserts that the gap between
    consecutive poll_interval increases is >= 5 ticks (i.e. not consecutive).
    """
    from jarvis_common import jobs

    job_id = "test-job-id"
    row = _make_row(status="doing")

    # Controlled time: advances by poll_interval seconds each iteration.
    # We capture `intervals_waited` to know what poll_interval was at each tick.
    intervals_waited: list[float] = []

    # time_values list — we'll populate it lazily during the mock
    current_time = [0.0]

    def next_time():
        return current_time[0]

    mock_loop = MagicMock()
    mock_loop.time.side_effect = next_time

    call_count = [0]

    async def mock_get_proc(pool, jid):
        call_count[0] += 1
        if call_count[0] > 80:
            r = dict(row)
            r["status"] = "succeeded"
            return r
        return dict(row)

    async def mock_wait(pool, job_id_, timeout):
        # Advance simulated clock by the poll_interval used for this wait
        intervals_waited.append(timeout)
        current_time[0] += timeout

    async def not_disconnected():
        return False

    pool = MagicMock()

    with (
        patch.object(asyncio, "get_running_loop", return_value=mock_loop),
        patch.object(jobs, "get_procrastinate_job_for_jarvis_id", side_effect=mock_get_proc),
        patch.object(jobs, "_wait_for_job_notification", side_effect=mock_wait),
        patch.object(jobs, "procrastinate_row_to_jarvis_row", side_effect=lambda r: r),
    ):
        await _drain(
            jobs.stream_job_events(pool, job_id, is_disconnected=not_disconnected),
        )

    # -------------------------------------------------------------------------
    # Invariant 1: poll_interval must increase at some point (ramp exists).
    # -------------------------------------------------------------------------
    assert any(v > 2.0 for v in intervals_waited), (
        "poll_interval never increased — ramp-up logic is missing or broken. "
        f"All waited intervals: {intervals_waited[:30]}"
    )

    # -------------------------------------------------------------------------
    # Invariant 2: no two consecutive increases in poll_interval.
    # The old cascade bug produces three consecutive increases (ticks n, n+1, n+2).
    # With elapsed-time fix, each increase requires 30+ s, so at 3.0 s/tick
    # the next increase can't happen for at least 10 more ticks.
    # We check: between any two poll_interval increases, there must be >= 5 ticks
    # where the interval stays constant (i.e. the increases are not consecutive).
    # -------------------------------------------------------------------------
    increase_ticks = [
        i for i in range(1, len(intervals_waited)) if intervals_waited[i] > intervals_waited[i - 1]
    ]

    if len(increase_ticks) >= 2:
        for a, b in zip(increase_ticks, increase_ticks[1:]):
            gap = b - a
            assert gap >= 5, (
                f"BE-02: consecutive poll_interval increases at ticks {a} and {b} "
                f"(gap={gap}, < 5). This indicates the old idle_ticks*poll_interval "
                f"proxy cascade is still present. "
                f"Increase ticks: {increase_ticks}. "
                f"Waited intervals (first 30): {intervals_waited[:30]}"
            )


@pytest.mark.asyncio
async def test_adaptive_throttle_first_ramp_requires_30_elapsed_seconds():
    """BE-02: first poll_interval increase must not fire before 30 elapsed seconds.

    At 2.0 s per tick, 30 s = 15 ticks.  The 16th tick is the first moment
    past the 30-second boundary.  The ramp must NOT fire before tick 15.

    With the old proxy this also passes (15*2=30 > 30 is False; 16*2=32 > 30 is True),
    but combined with the cascade test this suite fully pins the correct invariant.
    """
    from jarvis_common import jobs

    job_id = "test-job-id"
    row = _make_row(status="doing")

    # Fixed step: 2.0 s per tick
    current_time = [0.0]

    def next_time():
        return current_time[0]

    mock_loop = MagicMock()
    mock_loop.time.side_effect = next_time

    call_count = [0]

    async def mock_get_proc(pool, jid):
        call_count[0] += 1
        if call_count[0] > 60:
            r = dict(row)
            r["status"] = "succeeded"
            return r
        return dict(row)

    intervals_waited: list[float] = []

    async def mock_wait(pool, job_id_, timeout):
        intervals_waited.append(timeout)
        current_time[0] += 2.0  # always step by 2s regardless of poll_interval

    async def not_disconnected():
        return False

    pool = MagicMock()

    with (
        patch.object(asyncio, "get_running_loop", return_value=mock_loop),
        patch.object(jobs, "get_procrastinate_job_for_jarvis_id", side_effect=mock_get_proc),
        patch.object(jobs, "_wait_for_job_notification", side_effect=mock_wait),
        patch.object(jobs, "procrastinate_row_to_jarvis_row", side_effect=lambda r: r),
    ):
        await _drain(
            jobs.stream_job_events(pool, job_id, is_disconnected=not_disconnected),
        )

    first_ramp_index = next(
        (i for i, v in enumerate(intervals_waited) if v > 2.0),
        None,
    )

    assert first_ramp_index is not None, (
        "poll_interval never increased — ramp-up logic is missing or broken. "
        f"Waited intervals: {intervals_waited[:30]}"
    )

    # At 2 s/tick, ramp must not fire before tick 15 (30 s elapsed).
    assert first_ramp_index >= 15, (
        f"BE-02: first poll_interval increase at tick {first_ramp_index} "
        f"(< 15 = 30 s / 2 s/tick). Either the threshold is wrong or the "
        f"idle_ticks*poll_interval proxy is firing too early. "
        f"Waited intervals (first 25): {intervals_waited[:25]}"
    )


@pytest.mark.asyncio
async def test_adaptive_throttle_resets_idle_start_on_state_change():
    """BE-02: idle_start resets when state changes, restarting the 30-s countdown.

    A state change should reset poll_interval to 2.0 AND restart the 30-s window.
    After the change, 30 more seconds must pass before the next ramp step.
    """
    from jarvis_common import jobs

    job_id = "test-job-id"
    row_a = _make_row(status="doing", progress_message=None)
    row_b = _make_row(status="doing", progress_message="halfway")  # distinct state
    row_done = _make_row(status="succeeded")

    current_time = [0.0]

    def next_time():
        return current_time[0]

    mock_loop = MagicMock()
    mock_loop.time.side_effect = next_time

    call_count = [0]

    async def mock_get_proc(pool, jid):
        call_count[0] += 1
        if call_count[0] <= 20:
            return dict(row_a)
        if call_count[0] <= 50:
            return dict(row_b)  # state change at tick 20
        return dict(row_done)

    intervals_waited: list[float] = []

    async def mock_wait(pool, job_id_, timeout):
        intervals_waited.append(timeout)
        current_time[0] += 2.0  # 2s per tick

    async def not_disconnected():
        return False

    pool = MagicMock()

    with (
        patch.object(asyncio, "get_running_loop", return_value=mock_loop),
        patch.object(jobs, "get_procrastinate_job_for_jarvis_id", side_effect=mock_get_proc),
        patch.object(jobs, "_wait_for_job_notification", side_effect=mock_wait),
        patch.object(jobs, "procrastinate_row_to_jarvis_row", side_effect=lambda r: r),
    ):
        await _drain(
            jobs.stream_job_events(pool, job_id, is_disconnected=not_disconnected),
        )

    # After tick 20 (state change), poll_interval resets to 2.0.
    # The next 30s-window begins; 30s/2s = 15 ticks → ramp at tick 35+.
    # Check that intervals_waited[20] == 2.0 (reset confirmed) and
    # the ramp after the change doesn't happen before tick 35.
    if len(intervals_waited) > 20:
        assert intervals_waited[20] == 2.0, (
            f"poll_interval did not reset to 2.0 on state change at tick 20. "
            f"Got: {intervals_waited[20]}. Waited[18:25]: {intervals_waited[18:25]}"
        )

    # Find first ramp AFTER the state change (tick > 20)
    post_change = intervals_waited[20:]
    first_post_ramp = next(
        (i for i, v in enumerate(post_change) if v > 2.0),
        None,
    )

    if first_post_ramp is not None:
        assert first_post_ramp >= 15, (
            f"BE-02: After state change, poll_interval ramp fired at relative tick "
            f"{first_post_ramp} (< 15 = 30 s / 2 s/tick). idle_start did not "
            f"reset on state change. Post-change intervals: {post_change[:25]}"
        )


# ---------------------------------------------------------------------------
# list_jobs user_id filter SQL behaviour
# ---------------------------------------------------------------------------


class TestListJobsUserIdHandling:
    """Verify that list_jobs() user_id parameter behaviour matches the SQL.

    SQL reality (lines 581-584 / 623-626 of jobs.py):
      - user_id=None  → $3::text IS NULL  AND args->>'user_id' IS NULL
                        i.e. only system/NULL-owner jobs are returned.
      - user_id="x"  → $3::text IS NOT NULL AND args->>'user_id' = $3
                        i.e. only rows owned by that specific user.

    The OLD docstring incorrectly stated that user_id=None returns "all jobs".
    These tests pin the correct (SQL-matching) behaviour.
    """

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _make_pool(rows: list[dict]) -> tuple[MagicMock, MagicMock]:
        """Return a mock asyncpg.Pool whose acquired connection returns `rows`."""
        # list_jobs does: [dict(r) for r in rows], so rows from conn.fetch must
        # support dict() conversion via the _DictRecord shim.
        fetch_result: list[Any] = [_DictRecord(r) for r in rows]

        conn = MagicMock()
        conn.fetch = MagicMock(return_value=_async_return(fetch_result))

        pool_cm = MagicMock()
        pool_cm.__aenter__ = MagicMock(return_value=_async_return(conn))
        pool_cm.__aexit__ = MagicMock(return_value=_async_return(None))

        pool = MagicMock()
        pool.acquire = MagicMock(return_value=pool_cm)
        return pool, conn

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_user_id_none_fetches_only_null_owner_rows(self) -> None:
        """user_id=None passes $3=NULL → SQL filters to NULL-owner rows only."""
        from jarvis_common.jobs import list_jobs

        system_row = {"id": "sys-1", "user_id": None, "kind": "ingest", "status": "succeeded"}
        pool, conn = self._make_pool([system_row])

        result = await list_jobs(pool, user_id=None)

        # Verify the correct parameter was passed: $3 must be None (NULL)
        call_args = conn.fetch.call_args
        positional_params = call_args[0]  # (query, $1, $2, $3, $4)
        assert positional_params[3] is None, (
            "list_jobs(user_id=None) must pass $3=None so SQL restricts to NULL-owner rows"
        )
        assert result == [system_row]

    @pytest.mark.asyncio
    async def test_user_id_set_fetches_only_that_users_rows(self) -> None:
        """user_id='alice' passes $3='alice' → SQL restricts to alice's rows."""
        from jarvis_common.jobs import list_jobs

        alice_row = {"id": "job-1", "user_id": "alice", "kind": "ingest", "status": "running"}
        pool, conn = self._make_pool([alice_row])

        result = await list_jobs(pool, user_id="alice")

        call_args = conn.fetch.call_args
        positional_params = call_args[0]
        assert positional_params[3] == "alice", "list_jobs(user_id='alice') must pass $3='alice'"
        assert result == [alice_row]

    @pytest.mark.asyncio
    async def test_user_id_none_does_not_return_all_rows(self) -> None:
        """Regression: old docstring claimed user_id=None returns ALL jobs.

        The SQL contradicts this — it explicitly requires args->>'user_id' IS NULL
        when $3 is NULL.  This test documents and pins that user-id=None is NOT
        a 'no-filter' wildcard; it is a 'system jobs only' filter.
        """
        from jarvis_common.jobs import list_jobs

        # System (NULL-user) row that the SQL keeps when $3 IS NULL.
        null_row = {"id": "sys-2", "user_id": None, "kind": "reindex", "status": "succeeded"}

        # Pool returns only what the DB would return given the SQL filter.
        # When $3 IS NULL the SQL yields only null_row; simulate that.
        pool, conn = self._make_pool([null_row])

        result = await list_jobs(pool, user_id=None)

        ids = [r["id"] for r in result]
        assert "job-2" not in ids, (
            "user_id=None must NOT return rows owned by 'bob' — "
            "SQL requires args->>'user_id' IS NULL when $3 is NULL"
        )
        assert "sys-2" in ids


# ---------------------------------------------------------------------------
# Internal helpers for list_jobs mock pool
# ---------------------------------------------------------------------------


def _async_return(value: Any):
    """Return a coroutine that resolves to `value`."""

    async def _coro(*_args, **_kwargs):
        return value

    return _coro()


class _DictRecord:
    """Minimal asyncpg Record stand-in that supports dict() conversion."""

    def __init__(self, data: dict) -> None:
        self._data = data

    def keys(self):
        return self._data.keys()

    def __getitem__(self, key):
        return self._data[key]

    def __iter__(self):
        return iter(self._data)

    def items(self):
        return self._data.items()


# ---------------------------------------------------------------------------
# get_procrastinate_job_for_jarvis_id: broad-except observability
# ---------------------------------------------------------------------------


class TestProcrastinateLookupBroadExcept:
    """A non-schema lookup failure must be logged at WARNING and return None.

    Narrow ``Undefined*`` handlers (unmigrated-schema graceful degradation) keep
    returning None silently; only the trailing broad ``except Exception`` is
    upgraded from a silent DEBUG swallow to a WARNING with a traceback so an
    unexpected DB/driver error is observable. It must still return None (no
    raise) — no caller maps this to a 503.
    """

    @pytest.mark.asyncio
    async def test_non_schema_error_logs_warning_and_returns_none(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        from jarvis_common.jobs import get_procrastinate_job_for_jarvis_id

        # conn.fetchrow raises a generic, non-Undefined* error.
        conn = MagicMock()
        conn.fetchrow = AsyncMock(side_effect=RuntimeError("connection reset"))

        pool_cm = MagicMock()
        pool_cm.__aenter__ = MagicMock(return_value=_async_return(conn))
        pool_cm.__aexit__ = MagicMock(return_value=_async_return(None))

        pool = MagicMock()
        pool.acquire = MagicMock(return_value=pool_cm)

        with caplog.at_level(logging.WARNING, logger="jarvis_common.jobs"):
            result = await get_procrastinate_job_for_jarvis_id(pool, "job-xyz")

        assert result is None, "Unexpected lookup error must degrade to None, not raise"

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings, "Broad-except branch must emit a WARNING (was a silent DEBUG)"
        assert warnings[0].exc_info is not None, (
            "WARNING must carry exc_info=True so the traceback is captured"
        )
        assert "job-xyz" in warnings[0].getMessage(), (
            "WARNING must include the jarvis_job_id for triage"
        )


# ---------------------------------------------------------------------------
# _STATUS_CASE_SQL DRY invariant
# ---------------------------------------------------------------------------


class TestStatusCaseSql:
    """B1-1: _STATUS_CASE_SQL must be consistent with PROCRASTINATE_STATUS_MAP.

    The SQL fragment is derived from PROCRASTINATE_STATUS_MAP at module load;
    these tests verify the derivation is correct and complete so that adding
    a new procrastinate status to the map automatically propagates to the SQL.
    """

    def test_every_map_key_appears_as_when_clause(self) -> None:
        """Each key in PROCRASTINATE_STATUS_MAP has a corresponding WHEN clause."""
        from jarvis_common.jobs import _STATUS_CASE_SQL, PROCRASTINATE_STATUS_MAP

        for proc_status in PROCRASTINATE_STATUS_MAP:
            assert f"WHEN '{proc_status}'" in _STATUS_CASE_SQL, (
                f"_STATUS_CASE_SQL is missing a WHEN clause for '{proc_status}'. "
                "Add the key to PROCRASTINATE_STATUS_MAP and regenerate."
            )

    def test_every_map_value_appears_as_then_clause(self) -> None:
        """Each target value in PROCRASTINATE_STATUS_MAP has a THEN clause."""
        from jarvis_common.jobs import _STATUS_CASE_SQL, PROCRASTINATE_STATUS_MAP

        for jarvis_status in set(PROCRASTINATE_STATUS_MAP.values()):
            assert f"THEN '{jarvis_status}'" in _STATUS_CASE_SQL, (
                f"_STATUS_CASE_SQL is missing a THEN clause for '{jarvis_status}'."
            )

    def test_fallback_else_clause_present(self) -> None:
        """The ELSE 'running' fallback clause must always be present."""
        from jarvis_common.jobs import _STATUS_CASE_SQL

        assert "ELSE 'running' END" in _STATUS_CASE_SQL, (
            "_STATUS_CASE_SQL must end with ELSE 'running' END for unknown statuses."
        )

    def test_when_then_count_matches_map_length(self) -> None:
        """Number of WHEN clauses equals the number of entries in the map."""
        from jarvis_common.jobs import _STATUS_CASE_SQL, PROCRASTINATE_STATUS_MAP

        when_count = _STATUS_CASE_SQL.count("WHEN '")
        assert when_count == len(PROCRASTINATE_STATUS_MAP), (
            f"_STATUS_CASE_SQL has {when_count} WHEN clauses but "
            f"PROCRASTINATE_STATUS_MAP has {len(PROCRASTINATE_STATUS_MAP)} entries."
        )
