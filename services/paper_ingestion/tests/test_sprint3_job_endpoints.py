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


@pytest.fixture(autouse=True)
def _dev_mode_for_validation_assertions(monkeypatch):
    """SEC-107 redacts pydantic loc/errors in production mode; tests in this
    file assert on those details, so force DEV_MODE=true."""
    monkeypatch.setenv("DEV_MODE", "true")


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
    from unittest.mock import MagicMock

    import jarvis_common.task_registry as task_registry

    app, _pool = app_with_pool
    mock_task = MagicMock()
    defer_async = AsyncMock()
    mock_task.defer_async = defer_async
    with patch.dict(task_registry._TASK_MAP, {"paper.summarize": mock_task}):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/summarize/42")

    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "queued"
    assert body["job_id"]  # UUID assigned at runtime
    defer_async.assert_awaited_once()
    call_kwargs = defer_async.call_args.kwargs
    assert call_kwargs["paper_id"] == 42
    assert "job_id" in call_kwargs
    assert "user_id" in call_kwargs


async def test_extract_endpoint_enqueues_single_extraction_job(app_with_pool):
    """POST /api/papers/{paper_id}/extract returns a durable job id."""
    from unittest.mock import MagicMock

    import jarvis_common.task_registry as task_registry

    app, _pool = app_with_pool
    mock_task = MagicMock()
    defer_async = AsyncMock()
    mock_task.defer_async = defer_async
    with patch.dict(task_registry._TASK_MAP, {"extraction.single": mock_task}):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/papers/42/extract", json={"template_id": 3})

    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "queued"
    assert body["job_id"]  # UUID assigned at runtime
    defer_async.assert_awaited_once()
    call_kwargs = defer_async.call_args.kwargs
    assert call_kwargs["paper_id"] == 42
    assert call_kwargs["template_id"] == 3
    assert "job_id" in call_kwargs
    assert "user_id" in call_kwargs


async def test_scan_local_pdfs_endpoint_enqueues_job(app_with_pool):
    """POST /api/scan-local-pdfs returns a durable job id instead of blocking."""
    from unittest.mock import MagicMock

    import jarvis_common.task_registry as task_registry

    app, _pool = app_with_pool
    mock_task = MagicMock()
    defer_async = AsyncMock()
    mock_task.defer_async = defer_async
    with patch.dict(task_registry._TASK_MAP, {"papers.scan_local": mock_task}):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/scan-local-pdfs")

    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "queued"
    assert body["job_id"]  # UUID assigned at runtime
    defer_async.assert_awaited_once()
    call_kwargs = defer_async.call_args.kwargs
    assert "job_id" in call_kwargs
    assert "user_id" in call_kwargs


# ---------------------------------------------------------------------------
# PI-EDGE-002 — discriminated-union payload validation tests
# ---------------------------------------------------------------------------


async def test_create_job_rejects_unknown_kind(app_with_pool):
    """POST /api/jobs with an unknown kind returns 422."""
    app, _pool = app_with_pool
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/jobs",
            json={"kind": "bogus.kind", "payload": {}},
        )

    assert resp.status_code == 422
    body = resp.json()
    # The service's validation-error handler puts structured errors in ``errors``.
    errors = body.get("errors") or body.get("detail") or []
    errors_str = str(errors)
    # Discriminator error surfaces the invalid tag in the message.
    assert (
        "bogus.kind" in errors_str
        or "union_tag_invalid" in errors_str
        or "discriminator" in errors_str
    )


async def test_create_job_rejects_missing_paper_id_for_paper_process(app_with_pool):
    """POST /api/jobs kind=paper.process with empty payload returns 422."""
    app, _pool = app_with_pool
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/jobs",
            json={"kind": "paper.process", "payload": {}},
        )

    assert resp.status_code == 422
    body = resp.json()
    errors_str = str(body.get("errors") or body.get("detail") or [])
    assert "paper_id" in errors_str


async def test_create_job_rejects_string_paper_id(app_with_pool):
    """POST /api/jobs kind=paper.process with paper_id as string returns 422."""
    app, _pool = app_with_pool
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/jobs",
            json={"kind": "paper.process", "payload": {"paper_id": "not-an-int"}},
        )

    assert resp.status_code == 422
    body = resp.json()
    errors_str = str(body.get("errors") or body.get("detail") or [])
    # Pydantic reports the field that failed int coercion.
    assert "paper_id" in errors_str or "int_parsing" in errors_str
