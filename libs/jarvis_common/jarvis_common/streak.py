"""Streak computation — shared by executive and review routers."""

from datetime import datetime, timedelta


def compute_streak(events: list[datetime]) -> int:
    """Return consecutive-day streak count given event timestamps.

    Input order does not matter — the function deduplicates and sorts internally.
    """
    if not events:
        return 0
    dates = sorted({e.date() for e in events}, reverse=True)
    today = datetime.now().date()
    streak = 0
    expected = today
    # if no events today, streak starts yesterday
    if dates[0] != today:
        expected = today - timedelta(days=1)
    for d in dates:
        if d == expected:
            streak += 1
            expected -= timedelta(days=1)
        elif d < expected:
            break
    return streak
