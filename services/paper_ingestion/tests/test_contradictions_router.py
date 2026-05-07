"""API contract tests for contradiction endpoints."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from httpx import ASGITransport
from tests.conftest import FakeRecord


def _mock_pool() -> tuple[MagicMock, AsyncMock]:
    pool = MagicMock()
    conn = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire.return_value = ctx
    return pool, conn


@pytest.fixture()
def app_with_pool():
    """Create the paper_ingestion app with DB/auth dependencies overridden."""
    from jarvis_common.auth import verify_api_key
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app

    pool, conn = _mock_pool()
    app.state.db_pool = pool
    app.state.limiter.enabled = False

    async def override_db_pool():
        return pool

    async def override_api_key():
        return None

    app.dependency_overrides[get_db_pool] = override_db_pool
    app.dependency_overrides[verify_api_key] = override_api_key
    yield app, pool, conn
    app.dependency_overrides.clear()
    app.state.limiter.enabled = True


@pytest.mark.asyncio
async def test_get_contradictions_returns_verified_rows(app_with_pool):
    """GET /api/contradictions returns persisted contradiction rows."""
    app, _pool, conn = app_with_pool
    conn.fetch.return_value = [
        FakeRecord(
            {
                "id": 1,
                "paper_a_id": 10,
                "paper_b_id": 11,
                "paper_a_title": "Paper A",
                "paper_b_title": "Paper B",
                "finding_a": "A",
                "finding_b": "B",
                "quote_a": "quote A",
                "quote_b": "quote B",
                "page_a": 1,
                "page_b": 2,
                "contradiction_type": "result",
                "explanation": "Conflict",
                "confidence": 0.85,
                "status": "verified",
                "created_at": datetime.now(UTC),
                "total_count": 1,
            }
        )
    ]

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/contradictions?paper_id=10")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1
    assert body["contradictions"][0]["paper_a_title"] == "Paper A"
    assert body["contradictions"][0]["contradiction_type"] == "result"


@pytest.mark.asyncio
async def test_scan_contradictions_endpoint_enqueues_job(app_with_pool):
    """POST /api/contradictions/scan returns a durable job id."""
    import jarvis_common.task_registry as task_registry

    app, pool, _conn = app_with_pool
    mock_task = MagicMock()
    defer = AsyncMock()
    mock_task.defer_async = defer
    with patch.dict(task_registry.KIND_TO_TASK, {"contradictions.scan": mock_task}):
        with patch(
            "paper_ingestion.routers.contradictions.uuid.uuid4", return_value="job-contradictions"
        ):
            async with httpx.AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/api/contradictions/scan", json={"paper_id": 5, "limit": 9}
                )

    assert resp.status_code == 202
    assert resp.json() == {"job_id": "job-contradictions", "status": "queued"}
    defer.assert_awaited_once_with(job_id="job-contradictions", user_id=None, paper_id=5, limit=9)


@pytest.mark.asyncio
async def test_scan_paper_contradictions_endpoint_enqueues_scoped_job(app_with_pool):
    """POST /api/papers/{paper_id}/contradictions/scan scopes the job payload."""
    import jarvis_common.task_registry as task_registry

    app, pool, _conn = app_with_pool
    mock_task = MagicMock()
    defer = AsyncMock()
    mock_task.defer_async = defer
    with patch.dict(task_registry.KIND_TO_TASK, {"contradictions.scan": mock_task}):
        with patch(
            "paper_ingestion.routers.contradictions.uuid.uuid4",
            return_value="job-paper-contradictions",
        ):
            async with httpx.AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post("/api/papers/7/contradictions/scan", json={"limit": 12})

    assert resp.status_code == 202
    assert resp.json() == {"job_id": "job-paper-contradictions", "status": "queued"}
    defer.assert_awaited_once_with(
        job_id="job-paper-contradictions", user_id=None, paper_id=7, limit=12
    )
