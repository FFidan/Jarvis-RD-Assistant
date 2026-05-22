"""Dashboard domain contract tests — Phase B target row A32.

Survivor-of: test_dashboard_api.py mock-unit assertions for get_dashboard_metrics.
Carve-out: app.state.http_client is MagicMock (outbound HTTP).
"""

from __future__ import annotations

import pytest
import pytest_asyncio
import httpx

pytestmark = [pytest.mark.contract, pytest.mark.asyncio(loop_scope="session")]

_TEST_API_KEY = "dashboard-contract-key-phase-b-do-not-use-in-prod"


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


@pytest_asyncio.fixture(scope="function", loop_scope="session")
async def _pi_app_with_pool(contract_conn):
    from jarvis_common import current_user_id_strict_with_owner_override
    from jarvis_common.testing import SharedConnPool
    from paper_ingestion.main import app

    shared = SharedConnPool(contract_conn)
    original_pool = getattr(app.state, "db_pool", None)
    app.state.db_pool = shared

    removed_override = app.dependency_overrides.pop(
        current_user_id_strict_with_owner_override, None
    )
    had_override = removed_override is not None

    yield app

    if original_pool is None:
        if hasattr(app.state, "db_pool"):
            del app.state.db_pool
    else:
        app.state.db_pool = original_pool

    if had_override:
        app.dependency_overrides[current_user_id_strict_with_owner_override] = removed_override


def _make_client(app, cookie: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"X-API-Key": _TEST_API_KEY},
        cookies={"jarvis_session": cookie},
    )


# ---------------------------------------------------------------------------
# A32: GET /api/dashboard/metrics — aggregate counts scoped to current user
# ---------------------------------------------------------------------------


async def test_a32_dashboard_metrics_returns_all_fields(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
):
    """Covers map row A32: GET /api/dashboard/metrics returns all 7 aggregate fields.

    Verified: dashboard_api.py:29-130 get_dashboard_metrics at HEAD d21aaea8.
    Survivor-of (future Phase C): test_dashboard_api.py mock-unit tests.
    """
    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/dashboard/metrics")

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text[:300]}"
    body = resp.json()
    for field in (
        "total_papers",
        "unread_papers",
        "pending_papers",
        "due_cards",
        "active_projects",
        "topic_count",
        "nudge_count",
    ):
        assert field in body, f"Missing field {field!r} in dashboard metrics: {body}"
    # All counts must be non-negative integers
    for field in ("total_papers", "unread_papers", "pending_papers", "due_cards"):
        assert isinstance(body[field], int) and body[field] >= 0, (
            f"Field {field!r} must be non-negative int; got {body[field]!r}"
        )


async def test_a32_dashboard_metrics_total_papers_matches_user_library(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """Covers map row A32: total_papers equals actual user_library row count for user A.

    Verified: dashboard_api.py:46-49 COUNT(*) FROM user_library WHERE user_id=$1.
    """
    # Count user A's library rows directly
    db_count = await contract_conn.fetchval(
        "SELECT COUNT(*) FROM user_library WHERE user_id = $1",
        contract_two_users.user_a_id,
    )

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_a) as c:
        resp = await c.get("/api/dashboard/metrics")

    assert resp.status_code == 200
    body = resp.json()
    assert body["total_papers"] == db_count, (
        f"total_papers={body['total_papers']} != user_library count={db_count} for user A"
    )


async def test_a32_dashboard_metrics_user_b_gets_own_counts(
    contract_two_users,
    _pi_app_with_pool,
    _configure_api_key,
    contract_conn,
):
    """Covers map row A32: user B's metrics are scoped to user B's library (not user A's).

    Verified: dashboard_api.py:44 WHERE user_id=$1 user-scoped queries.
    """
    db_count_a = await contract_conn.fetchval(
        "SELECT COUNT(*) FROM user_library WHERE user_id = $1",
        contract_two_users.user_a_id,
    )
    db_count_b = await contract_conn.fetchval(
        "SELECT COUNT(*) FROM user_library WHERE user_id = $1",
        contract_two_users.user_b_id,
    )

    async with _make_client(_pi_app_with_pool, contract_two_users.cookie_b) as c:
        resp = await c.get("/api/dashboard/metrics")

    assert resp.status_code == 200
    body = resp.json()
    assert body["total_papers"] == db_count_b, (
        f"User B's total_papers={body['total_papers']} != {db_count_b}"
    )
    # If users have different counts, this proves scoping
    if db_count_a != db_count_b:
        assert body["total_papers"] != db_count_a, (
            f"User B is leaking user A's library count ({db_count_a}) — IDOR"
        )
