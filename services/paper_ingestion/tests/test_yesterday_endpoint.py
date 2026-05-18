"""Unit tests for the on-the-fly § Yesterday rollup (UI_v3 My-Day §3.2/§4.2).

Verifies the day-boundary maths (timezone offset → correct UTC window and
local yesterday date), user-scoping, and the empty / populated payloads.
Uses the ``__wrapped__`` pattern (same as test_journal_endpoints.py).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from paper_ingestion.routers import my_day

from tests.conftest import _make_pool_and_conn


def _mock_request():
    return MagicMock()


def _patch_uid(uid: int = 42):
    return patch(
        "paper_ingestion.routers.my_day.current_user_id_strict",
        new_callable=AsyncMock,
        return_value=uid,
    )


class _FrozenDateTime(datetime):
    """datetime subclass with a pinned ``now`` for boundary determinism."""

    _FIXED = datetime(2026, 5, 15, 2, 30, 0, tzinfo=UTC)  # 02:30 UTC on the 15th

    @classmethod
    def now(cls, tz=None):  # noqa: D102
        return cls._FIXED if tz is None else cls._FIXED.astimezone(tz)


@pytest.mark.asyncio
async def test_yesterday_utc_boundary_default_offset():
    """tz_offset=0: now=2026-05-15 02:30Z → yesterday = 2026-05-14, window
    [2026-05-14 00:00Z, 2026-05-15 00:00Z)."""
    pool, conn = _make_pool_and_conn()
    conn.fetch.side_effect = [
        [{"id": 1, "title": "done task", "status": "done"}],  # completed
        [],  # deferred
    ]
    conn.fetchrow.return_value = {"focus_hours": 3.5, "cards_reviewed": 12}

    with _patch_uid(42), patch("paper_ingestion.routers.my_day.datetime", _FrozenDateTime):
        result = await my_day.get_yesterday.__wrapped__(
            _mock_request(), tz_offset_minutes=0, db_pool=pool
        )

    assert result.date == date(2026, 5, 14)
    assert result.focused_hours == 3.5
    assert result.cards_reviewed == 12
    assert result.tasks_done == 1
    assert result.completed[0].id == 1

    completed_call = conn.fetch.await_args_list[0]
    _sql, uid, start_utc, end_utc = completed_call.args
    assert uid == 42  # user-scoped
    assert start_utc == datetime(2026, 5, 14, 0, 0, 0, tzinfo=UTC)
    assert end_utc == datetime(2026, 5, 15, 0, 0, 0, tzinfo=UTC)
    # daily_log keyed on the LOCAL yesterday date
    _lsql, log_uid, log_date = conn.fetchrow.await_args.args
    assert log_uid == 42
    assert log_date == date(2026, 5, 14)


@pytest.mark.asyncio
async def test_yesterday_positive_offset_shifts_window():
    """tz_offset=+120 (UTC+2): local now = 04:30 on the 15th → yesterday still
    2026-05-14 *locally*, but the UTC window is shifted back 2h:
    [2026-05-13 22:00Z, 2026-05-14 22:00Z)."""
    pool, conn = _make_pool_and_conn()
    conn.fetch.side_effect = [[], []]
    conn.fetchrow.return_value = None

    with _patch_uid(7), patch("paper_ingestion.routers.my_day.datetime", _FrozenDateTime):
        result = await my_day.get_yesterday.__wrapped__(
            _mock_request(), tz_offset_minutes=120, db_pool=pool
        )

    assert result.date == date(2026, 5, 14)
    assert result.focused_hours == 0.0
    assert result.cards_reviewed == 0
    assert result.tasks_done == 0
    _sql, _uid, start_utc, end_utc = conn.fetch.await_args_list[0].args
    assert start_utc == datetime(2026, 5, 13, 22, 0, 0, tzinfo=UTC)
    assert end_utc == datetime(2026, 5, 14, 22, 0, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_yesterday_negative_offset_rolls_local_day_back():
    """tz_offset=-300 (UTC-5): now=02:30Z → local = 21:30 on the 14th →
    local yesterday = 2026-05-13. Window shifted +5h:
    [2026-05-13 05:00Z, 2026-05-14 05:00Z)."""
    pool, conn = _make_pool_and_conn()
    conn.fetch.side_effect = [[], [{"id": 9, "title": "carry me", "status": "blocked"}]]
    conn.fetchrow.return_value = None

    with _patch_uid(3), patch("paper_ingestion.routers.my_day.datetime", _FrozenDateTime):
        result = await my_day.get_yesterday.__wrapped__(
            _mock_request(), tz_offset_minutes=-300, db_pool=pool
        )

    assert result.date == date(2026, 5, 13)
    assert [t.id for t in result.deferred] == [9]
    _sql, _uid, start_utc, end_utc = conn.fetch.await_args_list[0].args
    assert start_utc == datetime(2026, 5, 13, 5, 0, 0, tzinfo=UTC)
    assert end_utc == datetime(2026, 5, 14, 5, 0, 0, tzinfo=UTC)
