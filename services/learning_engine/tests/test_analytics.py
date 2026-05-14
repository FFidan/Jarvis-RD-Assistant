"""Tests for analytics router user-scope isolation (H8 audit fix).

Each of the four handlers (get_activity, get_reviews, get_retention,
get_llm_cost) must only return rows belonging to the calling user.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PATCH_BASE = "learning_engine.routers.analytics.current_user_id_or_none"


def _make_pool(rows: list) -> tuple[MagicMock, AsyncMock]:
    """Return (pool, conn) mocks where conn.fetch returns *rows*."""
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=rows)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx
    return pool, conn


class FakeRecord(dict):
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


_TODAY = datetime.now(UTC).date()


# ---------------------------------------------------------------------------
# Parametrised isolation tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler_name", "user_a_rows", "user_b_rows"),
    [
        (
            "get_activity",
            [
                FakeRecord(
                    log_date=_TODAY,
                    tasks_completed=5,
                    cards_reviewed=10,
                    papers_read=2,
                    focus_hours=3.0,
                    notes="user A note",
                )
            ],
            [],
        ),
        (
            "get_reviews",
            [FakeRecord(rating=4, count=3)],
            [],
        ),
        (
            "get_retention",
            [
                FakeRecord(
                    review_date=_TODAY,
                    total=8,
                    good_easy=6,
                    retention_pct=75.0,
                )
            ],
            [],
        ),
        (
            "get_llm_cost",
            [FakeRecord(day=_TODAY, total_cost=0.42, workflow="summaries")],
            [],
        ),
    ],
)
async def test_analytics_user_scope_isolation(handler_name, user_a_rows, user_b_rows):
    """User A's data is returned when called as user A; user B's call returns empty."""
    from learning_engine.routers import analytics

    handler = getattr(analytics, handler_name).__wrapped__

    # --- User A call: DB returns their rows ---
    pool_a, conn_a = _make_pool(user_a_rows)
    with patch(_PATCH_BASE, new=AsyncMock(return_value=1)):
        result_a = await handler(MagicMock(), days=30, db_pool=pool_a)

    assert len(result_a) == len(user_a_rows), (
        f"{handler_name}: user A should see {len(user_a_rows)} row(s), got {len(result_a)}"
    )

    # Verify the SQL passed to conn.fetch contains the user_id scoping clause
    sql_called = conn_a.fetch.call_args[0][0]
    assert "IS NOT DISTINCT FROM" in sql_called, (
        f"{handler_name}: SQL must use IS NOT DISTINCT FROM for user scoping"
    )
    # Verify user_id is the first bind parameter (position 1)
    assert conn_a.fetch.call_args[0][1] == 1, f"{handler_name}: first parameter must be user_id=1"

    # --- User B call: DB returns empty (simulates isolation at DB level) ---
    pool_b, _ = _make_pool(user_b_rows)
    with patch(_PATCH_BASE, new=AsyncMock(return_value=2)):
        result_b = await handler(MagicMock(), days=30, db_pool=pool_b)

    assert result_b == [], f"{handler_name}: user B should see no rows, got {result_b!r}"


# ---------------------------------------------------------------------------
# Verify user_id param is threaded as the first positional arg (not days)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "handler_name",
    ["get_activity", "get_reviews", "get_retention", "get_llm_cost"],
)
async def test_analytics_user_id_is_first_sql_param(handler_name):
    """Handler passes user_id as $1 and days as $2 to conn.fetch."""
    from learning_engine.routers import analytics

    handler = getattr(analytics, handler_name).__wrapped__
    pool, conn = _make_pool([])

    with patch(_PATCH_BASE, new=AsyncMock(return_value=42)):
        await handler(MagicMock(), days=7, db_pool=pool)

    positional = conn.fetch.call_args[0]
    # positional[0] is the SQL string, [1] is user_id, [2] is days
    assert positional[1] == 42, (
        f"{handler_name}: user_id must be the first SQL param ($1), got {positional[1]!r}"
    )
    assert positional[2] == 7, (
        f"{handler_name}: days must be the second SQL param ($2), got {positional[2]!r}"
    )
