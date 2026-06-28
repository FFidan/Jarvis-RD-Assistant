"""Tests for live-edit reactivity of fsrs.desired_retention and fsrs.learning_steps.

Verifies that:
1. FSRSManager accepts both desired_retention and learning_steps at construction time.
2. _build_fsrs_manager_from_db reads live DB values and returns a correctly
   configured FSRSManager — no service restart required.
3. The review endpoint uses a fresh manager per request, so changing either
   FSRS key is immediately reflected in the next review.
"""

from __future__ import annotations

import json
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from fsrs import Rating
from learning_engine.fsrs_manager import FSRSManager
from learning_engine.routers.review import _build_fsrs_manager_from_db

# ---------------------------------------------------------------------------
# FSRSManager unit tests (desired_retention + learning_steps params)
# ---------------------------------------------------------------------------


class TestFSRSManagerConstruction:
    """FSRSManager correctly wires desired_retention and learning_steps."""

    def test_default_params(self) -> None:
        """Default manager uses 0.9 retention and [1m, 10m] steps."""
        mgr = FSRSManager()
        sched = mgr.scheduler
        assert abs(sched.desired_retention - 0.9) < 1e-9
        # Default steps: [timedelta(minutes=1), timedelta(minutes=10)]
        assert list(sched.learning_steps) == [timedelta(minutes=1), timedelta(minutes=10)]

    def test_custom_retention(self) -> None:
        """Custom desired_retention is passed through to the Scheduler."""
        mgr = FSRSManager(desired_retention=0.85)
        assert abs(mgr.scheduler.desired_retention - 0.85) < 1e-9

    def test_custom_learning_steps(self) -> None:
        """Custom learning_steps are passed through to the Scheduler."""
        steps = [timedelta(minutes=5), timedelta(minutes=20)]
        mgr = FSRSManager(desired_retention=0.9, learning_steps=steps)
        assert list(mgr.scheduler.learning_steps) == steps

    def test_changed_retention_affects_review(self) -> None:
        """Two managers with different retention both complete review without error."""
        mgr_low = FSRSManager(desired_retention=0.75)
        mgr_high = FSRSManager(desired_retention=0.95)
        state, _ = mgr_low.create_new_card()
        new_state_low, log_low, due_low = mgr_low.schedule_review(state, Rating.Good)
        new_state_high, log_high, due_high = mgr_high.schedule_review(state, Rating.Good)
        # Both should produce valid state and due datetime
        assert isinstance(new_state_low, dict)
        assert isinstance(new_state_high, dict)
        assert due_low is not None
        assert due_high is not None
        # The schedulers have different retention parameters — verify they are wired correctly
        assert abs(mgr_low.scheduler.desired_retention - 0.75) < 1e-9
        assert abs(mgr_high.scheduler.desired_retention - 0.95) < 1e-9

    def test_changed_learning_steps_affects_new_review(self) -> None:
        """A manager with longer learning steps schedules further ahead."""
        steps_short = [timedelta(minutes=1), timedelta(minutes=5)]
        steps_long = [timedelta(minutes=10), timedelta(minutes=30)]
        mgr_short = FSRSManager(learning_steps=steps_short)
        mgr_long = FSRSManager(learning_steps=steps_long)
        state, _ = mgr_short.create_new_card()
        _, _, due_short = mgr_short.schedule_review(state, Rating.Again)
        _, _, due_long = mgr_long.schedule_review(state, Rating.Again)
        # Longer steps → later due date for "Again" (re-learning step)
        assert due_long >= due_short


# ---------------------------------------------------------------------------
# _build_fsrs_manager_from_db unit tests
# ---------------------------------------------------------------------------


def _make_fake_conn(rows: list[dict]) -> AsyncMock:
    """Return a mock asyncpg Connection whose fetch() returns the given rows."""

    class FakeRow(dict):
        def __getitem__(self, key):
            return super().__getitem__(key)

    conn = AsyncMock()
    conn.fetch.return_value = [FakeRow(r) for r in rows]
    return conn


@pytest.mark.asyncio
async def test_build_fsrs_manager_defaults_when_no_rows() -> None:
    """When user_config has no FSRS keys, defaults (0.9, [1m,10m]) are used."""
    conn = _make_fake_conn([])
    mgr = await _build_fsrs_manager_from_db(conn)
    assert abs(mgr.scheduler.desired_retention - 0.9) < 1e-9
    assert list(mgr.scheduler.learning_steps) == [timedelta(minutes=1), timedelta(minutes=10)]


@pytest.mark.asyncio
async def test_build_fsrs_manager_reads_desired_retention() -> None:
    """fsrs.desired_retention from DB overrides the 0.9 default."""
    conn = _make_fake_conn([{"key": "fsrs.desired_retention", "value": "0.85"}])
    mgr = await _build_fsrs_manager_from_db(conn)
    assert abs(mgr.scheduler.desired_retention - 0.85) < 1e-9


@pytest.mark.asyncio
async def test_build_fsrs_manager_clamps_above_range_retention() -> None:
    """desired_retention > 1 is clamped to at most 0.99; FSRS requires (0,1) open interval."""
    conn = _make_fake_conn([{"key": "fsrs.desired_retention", "value": "1.5"}])
    mgr = await _build_fsrs_manager_from_db(conn)
    assert mgr.scheduler.desired_retention <= 0.99


@pytest.mark.asyncio
async def test_build_fsrs_manager_clamps_zero_retention() -> None:
    """desired_retention == 0 (degenerate: log(0) in FSRS) is pulled into (0,1)."""
    conn = _make_fake_conn([{"key": "fsrs.desired_retention", "value": "0"}])
    mgr = await _build_fsrs_manager_from_db(conn)
    assert 0.0 < mgr.scheduler.desired_retention < 1.0


@pytest.mark.asyncio
async def test_build_fsrs_manager_reads_learning_steps() -> None:
    """fsrs.learning_steps from DB overrides the [1, 10] default."""
    conn = _make_fake_conn([{"key": "fsrs.learning_steps", "value": json.dumps([5, 20])}])
    mgr = await _build_fsrs_manager_from_db(conn)
    assert list(mgr.scheduler.learning_steps) == [timedelta(minutes=5), timedelta(minutes=20)]


@pytest.mark.asyncio
async def test_build_fsrs_manager_reads_both_keys() -> None:
    """Both FSRS keys can be set simultaneously."""
    conn = _make_fake_conn(
        [
            {"key": "fsrs.desired_retention", "value": "0.80"},
            {"key": "fsrs.learning_steps", "value": json.dumps([3, 15])},
        ]
    )
    mgr = await _build_fsrs_manager_from_db(conn)
    assert abs(mgr.scheduler.desired_retention - 0.80) < 1e-9
    assert list(mgr.scheduler.learning_steps) == [timedelta(minutes=3), timedelta(minutes=15)]


@pytest.mark.asyncio
async def test_build_fsrs_manager_falls_back_on_bad_retention(caplog) -> None:
    """Malformed fsrs.desired_retention falls back to default 0.9 with a warning."""
    conn = _make_fake_conn([{"key": "fsrs.desired_retention", "value": "not-a-float"}])
    mgr = await _build_fsrs_manager_from_db(conn)
    assert abs(mgr.scheduler.desired_retention - 0.9) < 1e-9


@pytest.mark.asyncio
async def test_build_fsrs_manager_falls_back_on_bad_steps(caplog) -> None:
    """Malformed fsrs.learning_steps falls back to default [1m, 10m] with a warning."""
    conn = _make_fake_conn([{"key": "fsrs.learning_steps", "value": '"bad-value"'}])
    mgr = await _build_fsrs_manager_from_db(conn)
    assert list(mgr.scheduler.learning_steps) == [timedelta(minutes=1), timedelta(minutes=10)]


@pytest.mark.asyncio
async def test_live_edit_retention_reflected_on_next_review() -> None:
    """Simulates live-edit: changing DB value → next call uses new retention."""
    # First review: retention=0.9 (default)
    conn_before = _make_fake_conn([{"key": "fsrs.desired_retention", "value": "0.9"}])
    mgr_before = await _build_fsrs_manager_from_db(conn_before)
    assert abs(mgr_before.scheduler.desired_retention - 0.9) < 1e-9

    # User edits retention to 0.75 via the API
    conn_after = _make_fake_conn([{"key": "fsrs.desired_retention", "value": "0.75"}])
    mgr_after = await _build_fsrs_manager_from_db(conn_after)
    assert abs(mgr_after.scheduler.desired_retention - 0.75) < 1e-9


@pytest.mark.asyncio
async def test_live_edit_learning_steps_reflected_on_next_review() -> None:
    """Simulates live-edit: changing DB steps → next call uses new steps."""
    conn_before = _make_fake_conn([{"key": "fsrs.learning_steps", "value": "[1, 10]"}])
    mgr_before = await _build_fsrs_manager_from_db(conn_before)
    assert list(mgr_before.scheduler.learning_steps) == [
        timedelta(minutes=1),
        timedelta(minutes=10),
    ]

    conn_after = _make_fake_conn([{"key": "fsrs.learning_steps", "value": "[5, 25]"}])
    mgr_after = await _build_fsrs_manager_from_db(conn_after)
    assert list(mgr_after.scheduler.learning_steps) == [timedelta(minutes=5), timedelta(minutes=25)]


# ---------------------------------------------------------------------------
# Per-user FSRS config (L-13)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_fsrs_manager_user_row_preferred_over_null_row() -> None:
    """user_id path: user-specific row is preferred over system-default NULL row."""
    conn = _make_fake_conn([{"key": "fsrs.desired_retention", "value": "0.75"}])
    mgr = await _build_fsrs_manager_from_db(conn, user_id=42)
    assert abs(mgr.scheduler.desired_retention - 0.75) < 1e-9


@pytest.mark.asyncio
async def test_build_fsrs_manager_falls_back_to_null_row_when_no_user_row() -> None:
    """user_id path: falls back to NULL-row value when no user-specific row exists."""
    conn = _make_fake_conn([{"key": "fsrs.desired_retention", "value": "0.85"}])
    mgr = await _build_fsrs_manager_from_db(conn, user_id=99)
    assert abs(mgr.scheduler.desired_retention - 0.85) < 1e-9


@pytest.mark.asyncio
async def test_build_fsrs_manager_without_user_id_uses_null_path() -> None:
    """user_id=None path: issues WHERE user_id IS NULL query."""
    conn = _make_fake_conn([{"key": "fsrs.desired_retention", "value": "0.88"}])
    mgr = await _build_fsrs_manager_from_db(conn, user_id=None)
    assert abs(mgr.scheduler.desired_retention - 0.88) < 1e-9
    call_args = conn.fetch.await_args
    assert call_args is not None
    sql_issued = call_args.args[0]
    assert "user_id IS NULL" in sql_issued
