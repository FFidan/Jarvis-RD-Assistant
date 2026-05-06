"""Tests for missing-foundational analytics endpoints."""

from __future__ import annotations

from unittest.mock import ANY, AsyncMock, MagicMock, patch

import httpx
import pytest
from httpx import ASGITransport


def _mock_pool() -> tuple[MagicMock, AsyncMock]:
    """Create a mocked asyncpg pool with one context-managed connection."""
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
    app.dependency_overrides[get_db_pool] = lambda: pool
    app.dependency_overrides[verify_api_key] = lambda: None
    yield app, pool, conn
    app.dependency_overrides.clear()
    app.state.limiter.enabled = True


@pytest.mark.asyncio
async def test_missing_foundational_returns_ranked_stub_rows(app_with_pool):
    """GET /api/analytics/missing-foundational returns ranked citation stubs."""
    app, _pool, conn = app_with_pool

    async def fetch_missing_foundational(query: str):
        assert "WHERE p.metadata->>'stub' = 'true'" in query
        assert "ORDER BY cited_by_library_count DESC" in query
        assert "p.citation_count DESC NULLS LAST" in query
        return [
            {
                "id": 2,
                "title": "Higher local citation paper",
                "authors": ["Author B"],
                "year": 2022,
                "citation_count": 100,
                "url": "https://example.test/b",
                "pdf_url": "https://example.test/b.pdf",
                "pdf_downloaded": False,
                "cited_by_library_count": 4,
            },
            {
                "id": 1,
                "title": "Higher global citation paper",
                "authors": ["Author A"],
                "year": 2021,
                "citation_count": 900,
                "url": "https://example.test/a",
                "pdf_url": None,
                "pdf_downloaded": False,
                "cited_by_library_count": 2,
            },
        ]

    conn.fetch.side_effect = fetch_missing_foundational

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/analytics/missing-foundational")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [row["paper_id"] for row in body] == [2, 1]
    assert body[0] == {
        "paper_id": 2,
        "title": "Higher local citation paper",
        "authors": ["Author B"],
        "year": 2022,
        "citation_count": 100,
        "cited_by_library_count": 4,
        "url": "https://example.test/b",
        "pdf_available": True,
    }
    assert body[1]["pdf_available"] is False


@pytest.mark.asyncio
async def test_fetch_and_process_local_pdf_promotes_stub_and_enqueues_process(app_with_pool):
    """A downloaded local PDF promotes the stub and queues paper.process."""
    app, pool, conn = app_with_pool
    conn.fetchrow.return_value = {
        "id": 42,
        "pdf_url": None,
        "pdf_downloaded": True,
        "pdf_local_path": "/shared/pdf_storage/paper.pdf",
        "metadata": {"stub": "true"},
    }

    with patch(
        "paper_ingestion.routers.analytics.paper_process.defer_async",
        new=AsyncMock(),
    ) as defer_async:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/analytics/fetch-and-process",
                json={"paper_id": 42},
            )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["paper_id"] == 42
    assert data["status"] == "queued"
    assert data["job_id"] is not None
    assert data["message"] is None
    conn.execute.assert_awaited_once()
    assert "UPDATE papers" in conn.execute.await_args.args[0]
    assert conn.execute.await_args.args[1] == 42
    defer_async.assert_awaited_once_with(job_id=ANY, user_id=None, paper_id=42)


@pytest.mark.asyncio
async def test_fetch_and_process_pdf_url_promotes_stub_and_enqueues_analyze(app_with_pool):
    """A remote PDF URL promotes the stub and queues paper.analyze."""
    app, pool, conn = app_with_pool
    conn.fetchrow.return_value = {
        "id": 43,
        "pdf_url": "https://example.test/paper.pdf",
        "pdf_downloaded": False,
        "pdf_local_path": None,
        "metadata": {"stub": "true"},
    }

    with patch(
        "paper_ingestion.routers.analytics.paper_analyze.defer_async",
        new=AsyncMock(),
    ) as defer_async:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/analytics/fetch-and-process",
                json={"paper_id": 43},
            )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["paper_id"] == 43
    assert data["status"] == "queued"
    assert data["job_id"] is not None
    assert data["message"] is None
    conn.execute.assert_awaited_once()
    defer_async.assert_awaited_once_with(job_id=ANY, user_id=None, paper_id=43)


@pytest.mark.asyncio
async def test_fetch_and_process_without_pdf_promotes_stub_but_does_not_enqueue(app_with_pool):
    """A stub with no local PDF and no PDF URL returns no_pdf without a job."""
    app, _pool, conn = app_with_pool
    conn.fetchrow.return_value = {
        "id": 44,
        "pdf_url": None,
        "pdf_downloaded": False,
        "pdf_local_path": None,
        "metadata": {"stub": "true"},
    }

    with (
        patch(
            "paper_ingestion.routers.analytics.paper_process.defer_async", new=AsyncMock()
        ) as defer_process,
        patch(
            "paper_ingestion.routers.analytics.paper_analyze.defer_async", new=AsyncMock()
        ) as defer_analyze,
    ):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/analytics/fetch-and-process",
                json={"paper_id": 44},
            )

    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "paper_id": 44,
        "status": "no_pdf",
        "job_id": None,
        "message": "No PDF URL is available for this citation stub.",
    }
    conn.execute.assert_awaited_once()
    defer_process.assert_not_awaited()
    defer_analyze.assert_not_awaited()


@pytest.mark.asyncio
async def test_fetch_and_process_missing_or_non_stub_row_returns_404(app_with_pool):
    """Missing rows and non-stub papers are rejected by the stub lookup."""
    app, _pool, conn = app_with_pool
    conn.fetchrow.return_value = None

    with (
        patch(
            "paper_ingestion.routers.analytics.paper_process.defer_async", new=AsyncMock()
        ) as defer_process,
        patch(
            "paper_ingestion.routers.analytics.paper_analyze.defer_async", new=AsyncMock()
        ) as defer_analyze,
    ):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/analytics/fetch-and-process",
                json={"paper_id": 999},
            )

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Citation stub paper not found"
    conn.execute.assert_not_awaited()
    defer_process.assert_not_awaited()
    defer_analyze.assert_not_awaited()


@pytest.mark.asyncio
async def test_feedback_summary_returns_correct_shape(app_with_pool):
    """GET /api/analytics/feedback-summary returns top_positive and top_negative lists."""
    app, _pool, conn = app_with_pool
    conn.fetch.return_value = [
        {"id": 1, "title": "Paper A", "positive_count": 5, "negative_count": 1},
        {"id": 2, "title": "Paper B", "positive_count": 0, "negative_count": 3},
        {"id": 3, "title": "Paper C", "positive_count": 2, "negative_count": 0},
    ]

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/analytics/feedback-summary")

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "top_positive" in data
    assert "top_negative" in data
    # Paper A and C have positive_count > 0
    positive_ids = {item["paper_id"] for item in data["top_positive"]}
    assert 1 in positive_ids
    assert 3 in positive_ids
    assert 2 not in positive_ids  # positive_count == 0 excluded
    # Paper A and B have negative_count > 0
    negative_ids = {item["paper_id"] for item in data["top_negative"]}
    assert 1 in negative_ids
    assert 2 in negative_ids
    assert 3 not in negative_ids  # negative_count == 0 excluded
    # Item shape
    for item in data["top_positive"]:
        assert "paper_id" in item
        assert "title" in item
        assert "count" in item
        assert item["count"] > 0


@pytest.mark.asyncio
async def test_feedback_summary_empty_table(app_with_pool):
    """Empty recommendation_feedback returns empty lists."""
    app, _pool, conn = app_with_pool
    conn.fetch.return_value = []

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/analytics/feedback-summary")

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["top_positive"] == []
    assert data["top_negative"] == []
