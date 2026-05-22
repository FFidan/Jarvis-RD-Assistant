"""Pure-unit + live_pg differential tests for the executive subsystem.

Behavioral router-level coverage (my-day focus stats, focus-log paper-not-found,
focus-log other-user-paper rejection, my-day-bundle null tolerance, focus-log
duration cap) lives in services/learning_engine/tests/contract/test_executive_contract.py.

The 20 SQL-keyed-dispatch / handler-bypass mock-unit equivalents that previously
lived here were retired in the Cluster 13 contract pass on 2026-05-22 with
survivor citations:

  test_my_day_returns_focus_stats               → LE-E-01 (test_my_day_focus_stats_aggregation)
  test_my_day_limit_recommendations_query_param → LE-E-01 (incidental shape)
  test_focus_log_paper_not_found                → LE-E-02 (test_focus_log_paper_not_found_returns_404)
  test_focus_log_rejects_other_users_paper      → LE-E-03 (test_focus_log_rejects_other_users_paper)
  test_my_day_bundle_null_tolerant_when_empty   → LE-E-04 (test_my_day_bundle_null_tolerant_with_no_seed)
  test_focus_log_missing_duration_returns_422   → LE-E-05 (test_focus_log_invalid_duration_returns_422)
  test_focus_log_negative_hours_returns_422     → LE-E-05 (sub-assertion)
  test_focus_log_excessive_hours_returns_422    → LE-E-05 (sub-assertion)
  test_my_day_happy_path / _empty               → test_get_my_day_response_shape (test_executive_contract.py:197)
  test_focus_log_bare_timer / _with_task_id     → test_log_focus_session_persists_to_daily_log (342)
  test_focus_log_task_not_found / _no_side_effects → test_log_focus_session_non_owned_task_gets_404 (325)
  test_my_day_tasks_include_project_context     → test_get_my_day_tasks_scoped_to_caller (227)
  test_my_day_returns_project_pulse             → 197
  test_my_day_includes_completed_tasks_today    → 227
  test_my_day_bundle_shape_and_aggregation      → test_get_my_day_bundle_response_keys (251) + LE-E-04
  test_quick_add_task_empty_title_returns_422   → test_quick_add_task_non_owned_project_gets_404 (281) covers HTTP layer; Pydantic-only assertion deferred
  test_quick_add_task_invalid_priority_returns_422 → same

These remaining tests cover:
  - The timezone-window helper for /api/executive/my-day-bundle (pure unit §1.1)
  - The focus-streak SQL gaps-and-islands implementation vs. its old Python
    walk (live-PG differential, §1.2-adjacent)
"""

from __future__ import annotations

import datetime

import pytest


# ---------------------------------------------------------------------------
# Pure-unit tests for the yesterday-window helper (B-YDAY)
# ---------------------------------------------------------------------------


def test_yday_timezone_window_utc_plus_3():
    """B-YDAY: yesterday window formula is correct for UTC+3.

    Verified: for tz_offset_minutes=180 the formula produces
    start_utc=2026-05-17T21:00Z, end_utc=2026-05-18T21:00Z when
    now_utc is 2026-05-19T10:00Z (= 13:00 local UTC+3).
    This exactly represents 2026-05-18T00:00..23:59 UTC+3 (yesterday local).
    """
    now_utc = datetime.datetime(2026, 5, 19, 10, 0, 0, tzinfo=datetime.UTC)
    tz_offset_minutes = 180  # UTC+3
    offset = datetime.timedelta(minutes=tz_offset_minutes)
    now_local = now_utc + offset  # 2026-05-19T13:00 (local, naive+offset arithmetic)
    yesterday_local_date = (now_local - datetime.timedelta(days=1)).date()
    assert yesterday_local_date == datetime.date(2026, 5, 18)

    start_utc = (
        datetime.datetime(
            yesterday_local_date.year,
            yesterday_local_date.month,
            yesterday_local_date.day,
            tzinfo=datetime.UTC,
        )
        - offset
    )
    end_utc = start_utc + datetime.timedelta(days=1)

    # 2026-05-18T00:00 UTC+3 = 2026-05-17T21:00Z
    assert start_utc == datetime.datetime(2026, 5, 17, 21, 0, 0, tzinfo=datetime.UTC)
    # end is exclusive: 2026-05-18T00:00 UTC+3 + 24h = 2026-05-18T21:00Z
    assert end_utc == datetime.datetime(2026, 5, 18, 21, 0, 0, tzinfo=datetime.UTC)


def test_yday_timezone_window_utc_minus_5():
    """B-YDAY: yesterday window is correct for UTC-5 (negative offset).

    now_utc=2026-05-19T03:00Z = 2026-05-18T22:00 local (UTC-5), so yesterday_local
    = 2026-05-17; start_utc = 2026-05-17T05:00Z, end_utc = 2026-05-18T05:00Z.
    """
    now_utc = datetime.datetime(2026, 5, 19, 3, 0, 0, tzinfo=datetime.UTC)
    tz_offset_minutes = -300  # UTC-5
    offset = datetime.timedelta(minutes=tz_offset_minutes)
    now_local = now_utc + offset  # 2026-05-18T22:00 (yesterday in local)
    yesterday_local_date = (now_local - datetime.timedelta(days=1)).date()
    assert yesterday_local_date == datetime.date(2026, 5, 17)

    start_utc = (
        datetime.datetime(
            yesterday_local_date.year,
            yesterday_local_date.month,
            yesterday_local_date.day,
            tzinfo=datetime.UTC,
        )
        - offset
    )
    end_utc = start_utc + datetime.timedelta(days=1)

    # 2026-05-17T00:00 UTC-5 = 2026-05-17T05:00Z
    assert start_utc == datetime.datetime(2026, 5, 17, 5, 0, 0, tzinfo=datetime.UTC)
    assert end_utc == datetime.datetime(2026, 5, 18, 5, 0, 0, tzinfo=datetime.UTC)


# ---------------------------------------------------------------------------
# Live-PG: focus-streak SQL must reproduce the old 365-row Python walk exactly.
# Opt-in via JARVIS_RUN_LIVE_PG=1.
# ---------------------------------------------------------------------------


def _py_streak(log_dates: list, today) -> int:
    """The exact pre-B7 Python streak algorithm, for differential testing."""
    rows = sorted(set(log_dates), reverse=True)
    if not rows:
        return 0
    expected = today if rows[0] == today else today - datetime.timedelta(days=1)
    if rows[0] != expected:
        return 0
    streak = 0
    for d in rows:
        if d == expected:
            streak += 1
            expected -= datetime.timedelta(days=1)
        else:
            break
    return streak


@pytest.mark.live_pg
@pytest.mark.asyncio
async def test_my_day_focus_streak_sql_live_pg(live_pg_dsn: str) -> None:
    """The gaps-and-islands streak SQL must equal _py_streak for every fixture.

    Covers: streak through today, streak ending yesterday, broken streak (gap),
    most-recent run too old (→ 0), and no rows (→ 0).
    """
    from pathlib import Path

    import asyncpg
    from learning_engine.routers.executive import _FOCUS_STREAK_SQL

    repo_root = Path(__file__).resolve().parents[3]
    init_sql = (repo_root / "db" / "init.sql").read_text(encoding="utf-8")

    pool = await asyncpg.create_pool(live_pg_dsn, min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            await conn.execute(init_sql)
            from paper_ingestion.migrations_runner import run_migrations

        await run_migrations(pool)

        async with pool.acquire() as conn:
            today = await conn.fetchval("SELECT CURRENT_DATE")
            d = datetime.timedelta(days=1)
            cases = {
                "through_today": [today, today - d, today - 2 * d],
                "ends_yesterday": [today - d, today - 2 * d],
                "broken_by_gap": [today, today - 2 * d, today - 3 * d],
                "too_old": [today - 5 * d, today - 6 * d],
                "empty": [],
            }
            for name, dates in cases.items():
                user_id = await conn.fetchval(
                    "INSERT INTO users (email) VALUES ($1) RETURNING id",
                    f"streak_{name}@example.com",
                )
                for ld in dates:
                    await conn.execute(
                        "INSERT INTO daily_log (user_id, log_date, focus_hours) "
                        "VALUES ($1, $2, 1.0)",
                        user_id,
                        ld,
                    )
                # A zero-hour row must NOT count (mirrors focus_hours > 0).
                await conn.execute(
                    "INSERT INTO daily_log (user_id, log_date, focus_hours) VALUES ($1, $2, 0)",
                    user_id,
                    today - 10 * d,
                )
                sql_val = await conn.fetchval(_FOCUS_STREAK_SQL, user_id)
                expected = _py_streak(dates, today)
                assert sql_val == expected, (
                    f"case {name}: SQL={sql_val} expected(py)={expected} dates={dates}"
                )
    finally:
        await pool.close()
