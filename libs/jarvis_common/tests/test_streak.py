"""Tests for jarvis_common.streak.compute_streak."""

from datetime import datetime, timedelta

from jarvis_common.streak import compute_streak


def _dt(days_ago: int = 0) -> datetime:
    """Return a datetime that is `days_ago` days before today (midnight UTC)."""
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    return today - timedelta(days=days_ago)


def test_empty_list_returns_zero():
    assert compute_streak([]) == 0


def test_single_event_today_returns_one():
    assert compute_streak([_dt(0)]) == 1


def test_three_consecutive_days_ending_today():
    events = [_dt(0), _dt(1), _dt(2)]
    assert compute_streak(events) == 3


def test_gap_in_middle_counts_up_to_gap():
    # Today + yesterday = 2; then a gap; then 3 days back
    events = [_dt(0), _dt(1), _dt(3), _dt(4)]
    assert compute_streak(events) == 2


def test_event_yesterday_only_returns_one():
    assert compute_streak([_dt(1)]) == 1


def test_unsorted_input_still_works():
    events = [_dt(2), _dt(0), _dt(1)]
    assert compute_streak(events) == 3


def test_duplicate_timestamps_deduplicated():
    events = [_dt(0), _dt(0), _dt(1)]
    assert compute_streak(events) == 2
