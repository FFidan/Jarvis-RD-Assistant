"""Tests for dashboard API endpoints (T3-4).

Covers:
- GET /api/dashboard/metrics — aggregate counts
- GET /api/papers/feed — enhanced filter parameters
- GET /api/papers/{id} — user_state in detail response
- PUT /api/papers/{id}/user-state — create/update user state
- CORS middleware headers
"""

# conftest.py has already installed tiktoken / qdrant_client / qdrant_client.models stubs.
import httpx
import pytest
from httpx import ASGITransport

try:  # FastAPI >=0.137 flattens the route tree through this public iterator.
    from fastapi.routing import (
        iter_route_contexts as _iter_route_contexts,  # type: ignore[attr-defined]
    )
except ImportError:  # FastAPI <0.137 keeps app.routes a flat APIRoute list.
    _iter_route_contexts = None

from jarvis_common.testing_contract_apps import PITestAppOptions, patch_pi_test_app

from tests.conftest import FakeRecord, _make_pool_and_conn

# ---------------------------------------------------------------------------
# Helpers — fake asyncpg records
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def _app():
    """Create a minimal app instance with mocked DB pool and disabled auth."""
    from jarvis_common import verify_api_key
    from jarvis_common.auth import current_user_id_strict
    from paper_ingestion.deps import get_db_pool, limiter
    from paper_ingestion.main import app

    mock_pool, conn = _make_pool_and_conn()
    with patch_pi_test_app(
        mock_pool,
        app=app,
        get_db_pool=get_db_pool,
        limiter=limiter,
        options=PITestAppOptions(
            remove_owner_override=False,
            override_db_dependency=True,
            disable_limiter=True,
            dependency_overrides={
                verify_api_key: lambda: None,
                current_user_id_strict: lambda: 1,
            },
        ),
    ):
        yield app, conn


# ---------------------------------------------------------------------------
# Tests: GET /api/dashboard/metrics
# ---------------------------------------------------------------------------


# Collapsed: test_dashboard_metrics_shape
# Survivor: test_dashboard_contract.py::test_a32_dashboard_metrics_returns_all_fields
# Contract A32 verifies all 7 field names present + non-negative ints with real DB data.


def test_dashboard_metrics_uses_shared_pool_dependency() -> None:
    """Dashboard metrics should declare get_db_pool as its injected DB dependency."""
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.routers.dashboard_api import router as dashboard_router

    route = next(
        route
        for route in dashboard_router.routes
        if route.path == "/api/dashboard/metrics" and "GET" in route.methods
    )
    dependency_calls = {dependency.call for dependency in route.dependant.dependencies}

    assert get_db_pool in dependency_calls


def test_feed_route_precedes_dynamic_paper_detail_route() -> None:
    """Static feed route must be registered before /api/papers/{paper_id}."""
    from paper_ingestion.main import app

    if _iter_route_contexts is not None:
        paths = [context.path for context in _iter_route_contexts(app.router.routes)]
    else:
        paths = [route.path for route in app.router.routes if hasattr(route, "path")]

    assert "/api/papers/feed" in paths
    assert "/api/papers/{paper_id}" in paths
    assert paths.index("/api/papers/feed") < paths.index("/api/papers/{paper_id}")


# ---------------------------------------------------------------------------
# Tests: GET /api/papers/feed with filters
# ---------------------------------------------------------------------------


# Cluster 8 deletions (2026-05-22):
#   test_feed_filter_by_source     → test_feed_contract.py::test_feed_filter_by_source_type_behavioral
#   test_feed_filter_by_text       → test_feed_contract.py::test_feed_filter_by_text_behavioral
#   test_feed_filter_by_date_range → test_feed_contract.py::test_feed_filter_by_date_range_behavioral
#   test_paper_detail_user_state_null_when_absent → test_papers_contract.py::test_paper_detail_null_user_state
# All four were SQL-substring or handler-bypass mock-units; real-DB contract equivalents
# now exist with behavioral assertions on the in/out result sets.


# ---------------------------------------------------------------------------
# Tests: PUT /api/papers/{paper_id}/user-state
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Tests: CORS headers
# ---------------------------------------------------------------------------


async def test_cors_headers_present(_app):
    """Responses include CORS access-control-allow-origin header."""
    app, conn = _app
    conn.fetchrow.return_value = FakeRecord(
        total_papers=0,
        unread_papers=0,
        pending_papers=0,
        due_cards=0,
        active_projects=0,
        topic_count=0,
        nudge_count=0,
        chunked_papers=0,
    )

    # CORSMiddleware is configured at app import time from CORS_ORIGINS env,
    # defaulting to ``https://localhost:3001``. Use that allowed origin and
    # assert the middleware echoes it back (not ``*``).
    allowed_origin = "https://localhost:3001"
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/api/dashboard/metrics",
            headers={"Origin": allowed_origin},
        )

    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == allowed_origin
