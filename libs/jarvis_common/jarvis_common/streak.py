"""Streak computation — shared by executive and review routers."""

from datetime import UTC, datetime, timedelta


def compute_streak(events: list[datetime]) -> int:
    """Return consecutive-day streak count given event timestamps.

    Day boundaries are anchored to **UTC**, not the host machine's local TZ.
    All persisted timestamps in JARVIS are TIMESTAMPTZ stored as UTC, so
    comparing against a UTC ``today`` keeps the streak deterministic across
    deployments running in different timezones (and avoids a 23-hour gap on
    DST transition days).

    Input order does not matter — the function deduplicates and sorts internally.
    """
    if not events:
        return 0
    dates = sorted({e.date() for e in events}, reverse=True)
    today = datetime.now(UTC).date()
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
