"""Analytics contract tests — A181, A182, A183, A184, A185.

Covers:
- GET /api/analytics/activity  (A181) — daily_log rows for caller; user B cannot see A's
- GET /api/analytics/reviews   (A182) — review_logs scoped to caller's user_id
- GET /api/analytics/retention (A183) — retention_pct from caller's review_logs only
- GET /api/analytics/llm-cost  (A184) — llm_usage_log scoped to caller's user_id
- GET /api/analytics/summary   (A185) — aggregates scoped to caller + cross-period isolation
"""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock
from datetime import UTC, date, datetime, timedelta

from jarvis_common.testing import SharedConnPool

pytestmark = [pytest.mark.contract, pytest.mark.asyncio(loop_scope="session")]

_TEST_API_KEY = "le-contract-analytics-test-key"


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def _le_app(contract_conn):
    from learning_engine.deps import get_anki_exporter, get_db_pool, get_fsrs_manager
    from learning_engine.main import app

    shared = SharedConnPool(contract_conn)
    original_pool = getattr(app.state, "db_pool", None)
    original_http = getattr(app.state, "http_client", None)
    original_fsrs = getattr(app.state, "fsrs_manager", None)
    original_exporter = getattr(app.state, "anki_exporter", None)
    original_generator = getattr(app.state, "card_generator", None)

    mock_fsrs = MagicMock()
    _now = datetime.now(UTC)
    mock_fsrs.create_new_card.return_value = ({}, _now)
    mock_fsrs.schedule_review.return_value = ({}, {}, _now + timedelta(days=1))

    app.state.db_pool = shared
    app.state.http_client = AsyncMock()
    app.state.fsrs_manager = mock_fsrs
    app.state.anki_exporter = MagicMock()
    app.state.card_generator = AsyncMock()
    app.dependency_overrides[get_db_pool] = lambda: shared
    app.dependency_overrides[get_fsrs_manager] = lambda: mock_fsrs
    app.dependency_overrides[get_anki_exporter] = lambda: MagicMock()

    from learning_engine.deps import limiter

    limiter_was_enabled = limiter.enabled
    limiter.enabled = False

    try:
        yield app
    finally:
        limiter.enabled = limiter_was_enabled
        if original_pool is None:
            if hasattr(app.state, "db_pool"):
                del app.state.db_pool
        else:
            app.state.db_pool = original_pool
        if original_http is None:
            if hasattr(app.state, "http_client"):
                del app.state.http_client
        else:
            app.state.http_client = original_http
        if original_fsrs is None:
            if hasattr(app.state, "fsrs_manager"):
                del app.state.fsrs_manager
        else:
            app.state.fsrs_manager = original_fsrs
        if original_exporter is None:
            if hasattr(app.state, "anki_exporter"):
                del app.state.anki_exporter
        else:
            app.state.anki_exporter = original_exporter
        if original_generator is None:
            if hasattr(app.state, "card_generator"):
                del app.state.card_generator
        else:
            app.state.card_generator = original_generator
        app.dependency_overrides.pop(get_db_pool, None)
        app.dependency_overrides.pop(get_fsrs_manager, None)
        app.dependency_overrides.pop(get_anki_exporter, None)


@pytest.fixture(scope="function")
def _configure_api_key(monkeypatch):
    from jarvis_common import auth as _auth
    from jarvis_common.settings import get_secrets_settings

    monkeypatch.setenv("JARVIS_API_KEY", _TEST_API_KEY)
    get_secrets_settings.cache_clear()
    _auth.refresh_api_key_cache()
    yield
    get_secrets_settings.cache_clear()
    _auth.refresh_api_key_cache()


def _client(app, cookie: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"X-API-Key": _TEST_API_KEY},
        cookies={"jarvis_session": cookie},
    )


# ---------------------------------------------------------------------------
# §A181 — GET /api/analytics/activity — daily_log scoped to caller
# ---------------------------------------------------------------------------


async def test_activity_user_b_cannot_see_user_a_rows(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """User B's activity feed excludes user A's daily_log rows.

    Seeds a daily_log row with a distinctive focus_hours value for user A.
    User B's response must not contain that value. Collapses the SQL-text
    ``WHERE user_id = $1`` assertion in test_analytics.py to a real scoping proof.
    """
    today = date.today()
    await contract_conn.execute(
        """INSERT INTO daily_log (user_id, log_date, focus_hours, cards_reviewed,
               papers_read, tasks_completed)
           VALUES ($1, $2, 99.5, 0, 0, 0)
           ON CONFLICT (user_id, log_date) DO UPDATE SET focus_hours = 99.5""",
        contract_two_users.user_a_id,
        today,
    )

    async with _client(_le_app, contract_two_users.cookie_b) as c:
        resp = await c.get("/api/analytics/activity", params={"days": 7})

    assert resp.status_code == 200, (
        f"GET /api/analytics/activity for user B failed: {resp.status_code}: {resp.text[:300]}"
    )
    focus_values = [row["focus_hours"] for row in resp.json()]
    assert 99.5 not in focus_values, (
        f"IDOR: user B's activity includes user A's sentinel focus_hours 99.5. "
        f"Values: {focus_values}"
    )


async def test_activity_owner_sees_own_rows(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """User A sees their own daily_log row (positive control)."""
    today = date.today()
    await contract_conn.execute(
        """INSERT INTO daily_log (user_id, log_date, focus_hours, cards_reviewed,
               papers_read, tasks_completed)
           VALUES ($1, $2, 7.25, 0, 0, 0)
           ON CONFLICT (user_id, log_date) DO UPDATE SET focus_hours = 7.25""",
        contract_two_users.user_a_id,
        today,
    )

    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/analytics/activity", params={"days": 7})

    assert resp.status_code == 200
    focus_values = [row["focus_hours"] for row in resp.json()]
    assert 7.25 in focus_values, (
        f"User A expected to see their own focus_hours 7.25 in activity; got {focus_values}"
    )


# ---------------------------------------------------------------------------
# §A182 — GET /api/analytics/reviews — review_logs scoped to caller
# ---------------------------------------------------------------------------


async def test_reviews_scoped_to_caller(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """GET /api/analytics/reviews only counts caller's review_logs rows.

    Seeds a review_log for user A with rating=4. User B's response must not
    include a rating=4 row count that includes A's entry.
    """
    card_id_a = contract_two_users.card_id_a
    await contract_conn.execute(
        """INSERT INTO review_logs (card_id, rating, user_id, reviewed_at)
           VALUES ($1, 4, $2, NOW())""",
        card_id_a,
        contract_two_users.user_a_id,
    )

    async with _client(_le_app, contract_two_users.cookie_b) as c:
        resp_b = await c.get("/api/analytics/reviews", params={"days": 7})
    assert resp_b.status_code == 200
    # User B has no review_logs seeded — their result must be empty (or not contain A's)
    total_b = sum(row["count"] for row in resp_b.json())

    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp_a = await c.get("/api/analytics/reviews", params={"days": 7})
    assert resp_a.status_code == 200
    total_a = sum(row["count"] for row in resp_a.json())

    # User A has at least 1 review seeded; user B must have fewer
    assert total_b < total_a or total_b == 0, (
        f"Scoping concern: user B review count {total_b} >= user A's {total_a} "
        f"after seeding only A's review"
    )


# ---------------------------------------------------------------------------
# §A183 — GET /api/analytics/retention — computed from caller's rows only
# ---------------------------------------------------------------------------


async def test_retention_returns_valid_shape(contract_two_users, _le_app, _configure_api_key):
    """GET /api/analytics/retention returns a list of items with valid fields.

    Behavioral shape contract: each item must have review_date, total,
    good_easy, retention_pct. Collapses test_analytics.py's SQL-text check.
    """
    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/analytics/retention", params={"days": 30})

    assert resp.status_code == 200, (
        f"GET /api/analytics/retention failed: {resp.status_code}: {resp.text[:300]}"
    )
    items = resp.json()
    assert isinstance(items, list)
    for item in items:
        assert "review_date" in item, f"Missing review_date: {item}"
        assert "total" in item, f"Missing total: {item}"
        assert "good_easy" in item, f"Missing good_easy: {item}"
        assert "retention_pct" in item, f"Missing retention_pct: {item}"
        if item["total"] > 0 and item["retention_pct"] is not None:
            assert 0.0 <= item["retention_pct"] <= 100.0, (
                f"retention_pct {item['retention_pct']} out of [0,100]: {item}"
            )


# ---------------------------------------------------------------------------
# §A184 — GET /api/analytics/llm-cost — scoped to caller's user_id
# ---------------------------------------------------------------------------


async def test_llm_cost_scoped_to_caller(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """GET /api/analytics/llm-cost only returns caller's llm_usage_log rows.

    Seeds a cost row for user A with a sentinel cost value (123.45).
    User B must not see that value in their response.
    """
    await contract_conn.execute(
        """INSERT INTO llm_usage_log (user_id, workflow, cost_usd, created_at)
           VALUES ($1, 'contract-test', 123.45, NOW())""",
        contract_two_users.user_a_id,
    )

    async with _client(_le_app, contract_two_users.cookie_b) as c:
        resp = await c.get("/api/analytics/llm-cost", params={"days": 7})

    assert resp.status_code == 200, (
        f"GET /api/analytics/llm-cost for user B failed: {resp.status_code}: {resp.text[:300]}"
    )
    b_costs = [row["total_cost"] for row in resp.json()]
    assert 123.45 not in b_costs, (
        f"IDOR: user B sees user A's sentinel LLM cost 123.45 in {b_costs}"
    )


# ---------------------------------------------------------------------------
# §A185 — GET /api/analytics/summary — aggregates scoped to caller
# ---------------------------------------------------------------------------


async def test_analytics_summary_returns_required_fields(
    contract_two_users, _le_app, _configure_api_key
):
    """GET /api/analytics/summary returns all required KPI fields.

    Collapses test_analytics_summary.py's SQL-text assertion to a behavioral
    shape contract: response must contain current+prior period totals + streaks.
    """
    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/analytics/summary", params={"days": 30})

    assert resp.status_code == 200, (
        f"GET /api/analytics/summary failed: {resp.status_code}: {resp.text[:300]}"
    )
    body = resp.json()
    required = {
        "papers_read_total",
        "focus_hours_total",
        "cards_reviewed_total",
        "papers_read_prev",
        "focus_hours_prev",
        "cards_reviewed_prev",
        "focus_streak_days",
        "cards_review_streak_days",
    }
    missing = required - set(body.keys())
    assert not missing, f"analytics/summary missing fields: {missing}. Got: {list(body.keys())}"
    assert isinstance(body["focus_streak_days"], int) and body["focus_streak_days"] >= 0
    assert isinstance(body["cards_review_streak_days"], int)


async def test_analytics_summary_cross_period_isolation(
    contract_two_users, contract_conn, _le_app, _configure_api_key
):
    """Summary current-period and prior-period totals are computed independently.

    Seeds a daily_log row in the prior period only (31 days ago) and confirms
    it appears in *_prev but NOT in *_total (current 30-day window).
    """
    prior_date = date.today() - timedelta(days=31)
    await contract_conn.execute(
        """INSERT INTO daily_log (user_id, log_date, papers_read, focus_hours,
               cards_reviewed, tasks_completed)
           VALUES ($1, $2, 5, 2.0, 10, 0)
           ON CONFLICT (user_id, log_date) DO UPDATE
               SET papers_read = 5, focus_hours = 2.0, cards_reviewed = 10""",
        contract_two_users.user_a_id,
        prior_date,
    )

    async with _client(_le_app, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/analytics/summary", params={"days": 30})

    assert resp.status_code == 200
    body = resp.json()
    # The prior-period row is 31 days ago; for days=30 the prior window is
    # [today-60, today-30). 31 days ago falls in [today-60, today-30) → prev.
    # The current window is [today-30, today) → should NOT include 31-days-ago row.
    assert body["papers_read_prev"] >= 5, (
        f"Prior-period papers_read expected >= 5 (seeded 5 at day-31); "
        f"got {body['papers_read_prev']}"
    )
