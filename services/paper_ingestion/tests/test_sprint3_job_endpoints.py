"""Sprint 3 job endpoint contract tests."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from httpx import ASGITransport


def _mock_pool() -> MagicMock:
    pool = MagicMock()
    conn = AsyncMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=False)
    pool.acquire.return_value = cm
    return pool


@pytest.fixture()
def app_with_pool():
    """Create the paper_ingestion app with DB/auth dependencies overridden."""
    from jarvis_common.auth import verify_api_key
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app

    pool = _mock_pool()
    app.state.db_pool = pool
    app.state.limiter.enabled = False
    app.dependency_overrides[get_db_pool] = lambda: pool
    app.dependency_overrides[verify_api_key] = lambda: None
    yield app, pool
    app.dependency_overrides.clear()
    app.state.limiter.enabled = True


async def test_summarize_endpoint_enqueues_job(app_with_pool):
    """POST /api/summarize/{paper_id} returns a durable job id."""
    app, _pool = app_with_pool
    with patch("paper_ingestion.routers.rag.jobs_lib.enqueue", new_callable=AsyncMock) as enqueue:
        enqueue.return_value = "job-summary"
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/summarize/42")

    assert resp.status_code == 202
    assert resp.json() == {"job_id": "job-summary", "status": "queued"}
    enqueue.assert_awaited_once_with(_pool, "paper.summarize", {"paper_id": 42})


async def test_extract_endpoint_enqueues_single_extraction_job(app_with_pool):
    """POST /api/papers/{paper_id}/extract returns a durable job id."""
    app, _pool = app_with_pool
    with patch(
        "paper_ingestion.routers.extractions.jobs_lib.enqueue", new_callable=AsyncMock
    ) as enqueue:
        enqueue.return_value = "job-extract"
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/papers/42/extract", json={"template_id": 3})

    assert resp.status_code == 202
    assert resp.json() == {"job_id": "job-extract", "status": "queued"}
    enqueue.assert_awaited_once_with(
        _pool,
        "extraction.single",
        {"paper_id": 42, "template_id": 3},
    )


async def test_scan_local_pdfs_endpoint_enqueues_job(app_with_pool):
    """POST /api/scan-local-pdfs returns a durable job id instead of blocking."""
    app, _pool = app_with_pool
    with patch("paper_ingestion.routers.pdf.jobs_lib.enqueue", new_callable=AsyncMock) as enqueue:
        enqueue.return_value = "job-scan"
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/scan-local-pdfs")

    assert resp.status_code == 202
    assert resp.json() == {"job_id": "job-scan", "status": "queued"}
    enqueue.assert_awaited_once_with(_pool, "papers.scan_local", {})
