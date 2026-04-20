"""Tests for dashboard API endpoints (T3-4).

Covers:
- GET /api/dashboard/metrics — aggregate counts
- GET /api/papers/feed — enhanced filter parameters
- GET /api/papers/{id} — user_state in detail response
- PUT /api/papers/{id}/user-state — create/update user state
- CORS middleware headers
"""

import sys
import types
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

# ---------------------------------------------------------------------------
# Stub heavy native modules that are unavailable outside Docker.
# Must happen before any ``import app.*`` that reaches pdf_processor.
# (fitz is already stubbed by conftest.py)
# ---------------------------------------------------------------------------
if "qdrant_client" not in sys.modules:
    fake_qdrant = types.ModuleType("qdrant_client")
    fake_qdrant.AsyncQdrantClient = MagicMock()
    sys.modules["qdrant_client"] = fake_qdrant
if "qdrant_client.models" not in sys.modules:
    fake_qdrant_models = types.ModuleType("qdrant_client.models")
    fake_qdrant_models.Distance = MagicMock()
    fake_qdrant_models.PointIdsList = MagicMock()
    fake_qdrant_models.PointStruct = MagicMock()
    fake_qdrant_models.VectorParams = MagicMock()
    sys.modules["qdrant_client.models"] = fake_qdrant_models
if "tiktoken" not in sys.modules:
    fake_tiktoken = types.ModuleType("tiktoken")
    fake_tiktoken.get_encoding = MagicMock(return_value=MagicMock())
    sys.modules["tiktoken"] = fake_tiktoken

import httpx
import pytest
from httpx import ASGITransport

# ---------------------------------------------------------------------------
# Helpers — fake asyncpg records
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 3, 1, tzinfo=UTC)


class FakeRecord(dict):
    """Dict subclass that supports both dict[key] and .keys() like asyncpg.Record."""

    def keys(self):
        return super().keys()


def _make_paper_record(
    paper_id: int = 1,
    source_type: str = "arxiv",
    summary_brief: str | None = "Brief summary",
    tldr: str | None = None,
    confidence: str | None = "HIGH",
    user_status: str | None = "new",
    rating: int | None = None,
) -> FakeRecord:
    """Return a dict mimicking an asyncpg Record for a joined feed row."""
    return FakeRecord(
        id=paper_id,
        external_id=f"arxiv:{paper_id}",
        source_type=source_type,
        title=f"Paper {paper_id}",
        authors=["Author A"],
        abstract="Abstract text",
        published_date=None,
        url=f"https://arxiv.org/abs/{paper_id}",
        pdf_url=None,
        pdf_local_path=None,
        pdf_downloaded=False,
        citation_count=0,
        metadata={},
        discovered_at=_NOW,
        created_at=_NOW,
        priority_score=None,
        summary_brief=summary_brief,
        tldr=tldr,
        confidence=confidence,
        user_status=user_status,
        rating=rating,
    )


def _make_detail_paper_record(paper_id: int = 1) -> FakeRecord:
    """Return a paper row for the detail endpoint."""
    return FakeRecord(
        id=paper_id,
        external_id=f"arxiv:{paper_id}",
        source_type="arxiv",
        title=f"Paper {paper_id}",
        authors=["Author A"],
        abstract="Abstract text",
        published_date=None,
        url=f"https://arxiv.org/abs/{paper_id}",
        pdf_url=None,
        pdf_local_path=None,
        pdf_downloaded=False,
        citation_count=0,
        metadata={},
        discovered_at=_NOW,
        created_at=_NOW,
        priority_score=None,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_pool_and_conn() -> tuple[MagicMock, AsyncMock]:
    """Create a mock asyncpg Pool whose ``acquire()`` returns an async CM.

    ``pool.acquire()`` must be usable as ``async with pool.acquire() as conn``
    (like a real asyncpg PoolAcquireContext) so ``acquire`` is a plain
    MagicMock (returns the context manager synchronously), and the context
    manager yields an AsyncMock connection.
    """
    conn = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)

    pool = MagicMock()
    pool.acquire.return_value = ctx
    return pool, conn


@pytest.fixture()
def _app():
    """Create a minimal app instance with mocked DB pool and disabled auth."""
    from app.deps import get_db_pool
    from app.main import app
    from jarvis_common import verify_api_key

    mock_pool, conn = _make_pool_and_conn()
    app.state.db_pool = mock_pool
    app.state.limiter.enabled = False

    # Dashboard routes now use the shared dependency from app.main/app.deps.
    app.dependency_overrides[get_db_pool] = lambda: mock_pool
    app.dependency_overrides[verify_api_key] = lambda: None
    yield app, conn
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Tests: GET /api/dashboard/metrics
# ---------------------------------------------------------------------------


async def test_dashboard_metrics_shape(_app):
    """GET /api/dashboard/metrics returns all 7 fields as ints."""
    app, conn = _app
    conn.fetchrow.return_value = FakeRecord(
        total_papers=10,
        unread_papers=5,
        pending_papers=3,
        due_cards=2,
        active_projects=1,
        topic_count=4,
        nudge_count=6,
    )

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/dashboard/metrics")

    assert resp.status_code == 200
    body = resp.json()

    expected_keys = {
        "total_papers",
        "unread_papers",
        "pending_papers",
        "due_cards",
        "active_projects",
        "topic_count",
        "nudge_count",
        "onboarding_stage",
    }
    assert set(body.keys()) == expected_keys
    for key in expected_keys - {"onboarding_stage"}:
        assert isinstance(body[key], int), f"{key} should be int"
    assert isinstance(body["onboarding_stage"], str)


def test_dashboard_metrics_uses_shared_pool_dependency() -> None:
    """Dashboard metrics should declare get_db_pool as its injected DB dependency."""
    from app.deps import get_db_pool
    from app.routers.dashboard_api import router as dashboard_router

    route = next(
        route
        for route in dashboard_router.routes
        if route.path == "/api/dashboard/metrics" and "GET" in route.methods
    )
    dependency_calls = {dependency.call for dependency in route.dependant.dependencies}

    assert get_db_pool in dependency_calls


def test_feed_route_precedes_dynamic_paper_detail_route() -> None:
    """Static feed route must be registered before /api/papers/{paper_id}."""
    from app.main import app

    paths = [route.path for route in app.router.routes if hasattr(route, "path")]

    assert "/api/papers/feed" in paths
    assert "/api/papers/{paper_id}" in paths
    assert paths.index("/api/papers/feed") < paths.index("/api/papers/{paper_id}")


# ---------------------------------------------------------------------------
# Tests: GET /api/papers/feed with filters
# ---------------------------------------------------------------------------


async def test_feed_filter_by_status(_app):
    """GET /api/papers/feed?statuses=new filters by user status."""
    app, conn = _app
    records = [_make_paper_record(paper_id=1, user_status="new")]
    conn.fetch.return_value = records
    conn.fetchval.return_value = 1

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/papers/feed", params={"statuses": "new"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1

    # Verify SQL contains IN clause for status filter
    fetch_call = conn.fetch.call_args
    sql = fetch_call[0][0]
    assert "COALESCE(pus.status, 'new') IN" in sql


async def test_feed_filter_by_source(_app):
    """GET /api/papers/feed?source_types=semantic_scholar filters by source."""
    app, conn = _app
    records = [_make_paper_record(paper_id=1, source_type="semantic_scholar")]
    conn.fetch.return_value = records
    conn.fetchval.return_value = 1

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/papers/feed", params={"source_types": "semantic_scholar"})

    assert resp.status_code == 200
    fetch_call = conn.fetch.call_args
    sql = fetch_call[0][0]
    assert "p.source_type IN" in sql


async def test_feed_filter_by_text(_app):
    """GET /api/papers/feed?q=attention uses full-text search."""
    app, conn = _app
    conn.fetch.return_value = [_make_paper_record()]
    conn.fetchval.return_value = 1

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/papers/feed", params={"q": "attention"})

    assert resp.status_code == 200
    fetch_call = conn.fetch.call_args
    sql = fetch_call[0][0]
    assert "plainto_tsquery" in sql


async def test_feed_filter_by_date_range(_app):
    """GET /api/papers/feed?date_from=2026-01-01&date_to=2026-03-01 filters by date."""
    app, conn = _app
    conn.fetch.return_value = [_make_paper_record()]
    conn.fetchval.return_value = 1

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/api/papers/feed",
            params={"date_from": "2026-01-01", "date_to": "2026-03-01"},
        )

    assert resp.status_code == 200
    fetch_call = conn.fetch.call_args
    sql = fetch_call[0][0]
    assert "p.created_at >=" in sql
    assert "p.created_at <=" in sql


async def test_feed_combined_filters(_app):
    """GET /api/papers/feed with multiple filters combines them with AND."""
    app, conn = _app
    conn.fetch.return_value = [_make_paper_record()]
    conn.fetchval.return_value = 1

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/api/papers/feed",
            params={
                "q": "transformers",
                "statuses": "new,reading",
                "source_types": "arxiv",
            },
        )

    assert resp.status_code == 200
    fetch_call = conn.fetch.call_args
    sql = fetch_call[0][0]
    assert "plainto_tsquery" in sql
    assert "COALESCE(pus.status, 'new') IN" in sql
    assert "p.source_type IN" in sql


# ---------------------------------------------------------------------------
# Tests: GET /api/papers/{paper_id} — user_state
# ---------------------------------------------------------------------------


async def test_paper_detail_includes_user_state(_app):
    """GET /api/papers/{id} includes user_state when present."""
    app, conn = _app

    paper_row = _make_detail_paper_record(paper_id=1)
    user_state_row = FakeRecord(status="reading", rating=4, user_notes="Great paper", flagged=False)

    # fetchrow calls: paper, summary, user_state
    conn.fetchrow.side_effect = [paper_row, None, user_state_row]
    conn.fetch.return_value = []  # no chunks

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/papers/1")

    assert resp.status_code == 200
    body = resp.json()
    assert "user_state" in body
    assert body["user_state"] is not None
    assert body["user_state"]["status"] == "reading"
    assert body["user_state"]["rating"] == 4


async def test_paper_detail_user_state_null_when_absent(_app):
    """GET /api/papers/{id} returns user_state=null when no state exists."""
    app, conn = _app

    paper_row = _make_detail_paper_record(paper_id=1)

    conn.fetchrow.side_effect = [paper_row, None, None]
    conn.fetch.return_value = []

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/papers/1")

    assert resp.status_code == 200
    body = resp.json()
    assert body["user_state"] is None


# ---------------------------------------------------------------------------
# Tests: PUT /api/papers/{paper_id}/user-state
# ---------------------------------------------------------------------------


async def test_user_state_create(_app):
    """PUT /api/papers/{id}/user-state creates new user state."""
    app, conn = _app
    conn.fetchrow.return_value = FakeRecord(
        status="reading", rating=None, user_notes="Starting to read", flagged=False
    )

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.put(
            "/api/papers/1/user-state",
            json={"status": "reading", "user_notes": "Starting to read"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "reading"
    assert body["user_notes"] == "Starting to read"


async def test_user_state_update(_app):
    """PUT /api/papers/{id}/user-state updates existing state."""
    app, conn = _app
    conn.fetchrow.return_value = FakeRecord(
        status="read", rating=5, user_notes="Excellent paper", flagged=True
    )

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.put(
            "/api/papers/1/user-state",
            json={"status": "read", "rating": 5, "flagged": True},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "read"
    assert body["rating"] == 5
    assert body["flagged"] is True


def test_user_state_uses_shared_pool_dependency() -> None:
    """User-state writes should declare get_db_pool as their injected DB dependency."""
    from app.deps import get_db_pool
    from app.routers.dashboard_api import router as dashboard_router

    route = next(
        route
        for route in dashboard_router.routes
        if route.path == "/api/papers/{paper_id}/user-state" and "PUT" in route.methods
    )
    dependency_calls = {dependency.call for dependency in route.dependant.dependencies}

    assert get_db_pool in dependency_calls


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
    )

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/api/dashboard/metrics",
            headers={"Origin": "http://localhost:3000"},
        )

    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == "*"
