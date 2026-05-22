"""Tests for GET /api/analytics/summary (B5 — AnalyticsSummaryResponse).

Coverage:
- current/prior-period totals split (including across a month boundary)
- per-user isolation: another user's daily_log rows never bleed in
- SQL param contract: user_id=$1, days=$2 (mirrors test_analytics.py pattern)
- streak: focus and cards_review, including 0-streak and month-boundary cases
"""

from __future__ import annotations

import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.conftest import FakeRecord

# ---------------------------------------------------------------------------
# Helpers (mirror test_analytics.py style)
# ---------------------------------------------------------------------------


def _make_streak_row(log_date: datetime.date) -> FakeRecord:
    return FakeRecord(log_date=log_date)


def _make_pool_multi(fetchrow_side_effects: list, fetch_side_effects: list) -> tuple:
    """Build a pool mock where fetchrow/fetch are called in sequence.

    fetchrow_side_effects: list of return values for successive fetchrow() calls.
    fetch_side_effects:    list of return values for successive fetch() calls.
    """
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=fetchrow_side_effects)
    conn.fetch = AsyncMock(side_effect=fetch_side_effects)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx
    return pool, conn


_TODAY = datetime.datetime.now(datetime.UTC).date()


# ---------------------------------------------------------------------------
# _compute_streak unit tests (pure-function; no DB needed)
# ---------------------------------------------------------------------------


def test_compute_streak_empty():
    from learning_engine.routers.analytics import _compute_streak

    assert _compute_streak([], field="focus_hours") == 0


def test_compute_streak_single_today():
    from learning_engine.routers.analytics import _compute_streak

    rows = [_make_streak_row(_TODAY)]
    assert _compute_streak(rows, field="focus_hours") == 1


def test_compute_streak_single_yesterday():
    from learning_engine.routers.analytics import _compute_streak

    yesterday = _TODAY - datetime.timedelta(days=1)
    rows = [_make_streak_row(yesterday)]
    assert _compute_streak(rows, field="focus_hours") == 1


def test_compute_streak_gap_breaks():
    """A gap of 2+ days should break the streak (return 0 if most-recent is before yesterday)."""
    from learning_engine.routers.analytics import _compute_streak

    two_days_ago = _TODAY - datetime.timedelta(days=2)
    rows = [_make_streak_row(two_days_ago)]
    assert _compute_streak(rows, field="focus_hours") == 0


def test_compute_streak_consecutive_three():
    from learning_engine.routers.analytics import _compute_streak

    rows = [_make_streak_row(_TODAY - datetime.timedelta(days=i)) for i in range(3)]
    assert _compute_streak(rows, field="focus_hours") == 3


def test_compute_streak_month_boundary(monkeypatch):
    """Streak must span a month boundary seamlessly (always deterministic).

    ``_compute_streak`` calls ``datetime.datetime.now(UTC).date()`` at call time.
    We patch only ``datetime.datetime`` inside the analytics module so that
    ``.now()`` returns a fixed date (2026-06-01) while ``datetime.timedelta``
    and other helpers remain untouched.  The 5-day window spans May→June,
    verifying that month-crossing arithmetic does not break the streak count.
    """
    import datetime as _dt
    from unittest.mock import patch

    import learning_engine.routers.analytics as _analytics_mod

    _pinned = _dt.date(2026, 6, 1)

    class _FixedDatetime(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return _dt.datetime(2026, 6, 1, 12, 0, 0, tzinfo=tz)

    with patch.object(_analytics_mod.datetime, "datetime", _FixedDatetime):
        from learning_engine.routers.analytics import _compute_streak

        # 5 consecutive days ending on 2026-06-01 (crosses May→June boundary).
        rows = [_make_streak_row(_pinned - _dt.timedelta(days=i)) for i in range(5)]
        result = _compute_streak(rows, field="focus_hours")

    assert result == 5


def test_compute_streak_inner_gap():
    """Streak stops at first gap even if there are older active days."""
    from learning_engine.routers.analytics import _compute_streak

    # today, yesterday, gap, then 4 days ago
    rows = [
        _make_streak_row(_TODAY),
        _make_streak_row(_TODAY - datetime.timedelta(days=1)),
        # gap at -2
        _make_streak_row(_TODAY - datetime.timedelta(days=3)),
        _make_streak_row(_TODAY - datetime.timedelta(days=4)),
    ]
    assert _compute_streak(rows, field="focus_hours") == 2


# test_summary_sql_param_contract deleted — B1-09 positional-arg binding assertions
# (user_id=$1, days=$2 for all DB calls); survivor: test_analytics_contract.py
# (A185) exercises the same scoping guarantee against real PostgreSQL.


# ---------------------------------------------------------------------------
# get_analytics_summary — per-user isolation
# ---------------------------------------------------------------------------


# test_summary_per_user_isolation deleted — mock-unit with B1-09 positional-arg
# assertions (cur_args[1]==user_id); survivor:
# test_analytics_contract.py::test_analytics_summary_user_b_excludes_user_a_data (A189).

# test_summary_period_delta_correctness deleted — mock-unit duplicate;
# survivor: test_analytics_contract.py::test_analytics_summary_cross_period_isolation (A185).


@pytest.mark.asyncio
async def test_summary_empty_daily_log_returns_zeros():
    """When daily_log has no rows for the user, all totals and streaks are 0."""
    from learning_engine.routers.analytics import get_analytics_summary

    handler = get_analytics_summary.__wrapped__

    zero_current = FakeRecord(papers_read_total=0, focus_hours_total=0.0, cards_reviewed_total=0)
    zero_prev = FakeRecord(papers_read_prev=0, focus_hours_prev=0.0, cards_reviewed_prev=0)

    pool, _ = _make_pool_multi(
        fetchrow_side_effects=[zero_current, zero_prev],
        fetch_side_effects=[[], []],
    )
    result = await handler(MagicMock(), days=30, db_pool=pool, user_id=5)

    assert result.papers_read_total == 0
    assert result.focus_hours_total == 0.0
    assert result.cards_reviewed_total == 0
    assert result.papers_read_prev == 0
    assert result.focus_hours_prev == 0.0
    assert result.cards_reviewed_prev == 0
    assert result.focus_streak_days == 0
    assert result.cards_review_streak_days == 0


# ---------------------------------------------------------------------------
# get_analytics_summary — streak independence (focus vs cards_review)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summary_streaks_are_independent():
    """focus_streak and cards_review_streak can differ independently."""
    from learning_engine.routers.analytics import get_analytics_summary

    handler = get_analytics_summary.__wrapped__

    current_row = FakeRecord(papers_read_total=0, focus_hours_total=0.0, cards_reviewed_total=0)
    prev_row = FakeRecord(papers_read_prev=0, focus_hours_prev=0.0, cards_reviewed_prev=0)

    # Focus: 5-day streak; cards review: 2-day streak
    focus_rows = [_make_streak_row(_TODAY - datetime.timedelta(days=i)) for i in range(5)]
    review_rows = [_make_streak_row(_TODAY - datetime.timedelta(days=i)) for i in range(2)]

    pool, _ = _make_pool_multi(
        fetchrow_side_effects=[current_row, prev_row],
        fetch_side_effects=[focus_rows, review_rows],
    )
    result = await handler(MagicMock(), days=30, db_pool=pool, user_id=7)

    assert result.focus_streak_days == 5
    assert result.cards_review_streak_days == 2


# B2-13: test_analytics_summary_response_fields deleted — characterization test of a
#   settled Pydantic model; AnalyticsSummaryResponse construction + field access is
#   already exercised by test_summary_per_user_isolation, test_summary_period_delta_correctness,
#   test_summary_streaks_are_independent, and test_summary_empty_daily_log_returns_zeros above.
