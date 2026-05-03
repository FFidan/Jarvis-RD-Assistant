"""Zotero router tests.

Covers:
- PI-005: POST /api/zotero/poll enqueues a zotero.sync_from_zotero job and
  returns 200 with {job_id, status}.
- Import smoke-tests for zotero modules.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from httpx import ASGITransport

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pool_and_conn():
    """Create a mock asyncpg Pool whose acquire() returns an async context manager."""
    conn = AsyncMock()
    txn_ctx = MagicMock()
    txn_ctx.__aenter__ = AsyncMock(return_value=None)
    txn_ctx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn_ctx)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx
    return pool, conn


# ---------------------------------------------------------------------------
# Smoke-tests (module imports)
# ---------------------------------------------------------------------------


def test_zotero_imports():
    """Zotero client and service modules import without errors."""
    from paper_ingestion.integrations import zotero_client, zotero_service

    assert zotero_client is not None
    assert zotero_service is not None


def test_zotero_client_class_exists():
    """ZoteroClient class is importable and has expected public methods."""
    from paper_ingestion.integrations.zotero_client import ZoteroClient

    for method in (
        "create_item",
        "search_by_doi",
        "ensure_collection",
        "fetch_bbt_citation_key",
        "test_connection",
    ):
        assert hasattr(ZoteroClient, method), f"ZoteroClient missing method: {method}"


def test_zotero_service_functions_exist():
    """push_paper_to_zotero and resync_paper_to_zotero are importable callables."""
    import inspect

    from paper_ingestion.integrations.zotero_service import (
        push_paper_to_zotero,
        resync_paper_to_zotero,
    )

    assert inspect.iscoroutinefunction(push_paper_to_zotero)
    assert inspect.iscoroutinefunction(resync_paper_to_zotero)


# ---------------------------------------------------------------------------
# PI-005: POST /api/zotero/poll
# ---------------------------------------------------------------------------


@pytest.fixture()
def _app():
    """FastAPI app with mocked DB pool, bypassed API-key auth, and disabled rate limiter."""
    from jarvis_common import verify_api_key
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app

    mock_pool, conn = _make_pool_and_conn()
    app.state.db_pool = mock_pool
    app.state.limiter.enabled = False

    app.dependency_overrides[get_db_pool] = lambda: mock_pool
    app.dependency_overrides[verify_api_key] = lambda: None
    yield app, conn
    app.dependency_overrides.clear()
    app.state.limiter.enabled = True


@pytest.mark.asyncio
async def test_poll_now_enqueues_job(_app):
    """POST /api/zotero/poll enqueues a zotero.sync_from_zotero job and returns 200."""
    app, _conn = _app

    with patch(
        "jarvis_common.task_registry.zotero_sync_from_zotero.defer_async",
        new=AsyncMock(return_value=None),
    ) as mock_defer:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/zotero/poll",
                headers={"X-API-Key": "test"},
            )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert isinstance(body["job_id"], str)
    assert body["status"] == "queued"
    mock_defer.assert_awaited_once()
    call_kwargs = mock_defer.call_args.kwargs
    assert "job_id" in call_kwargs
    assert call_kwargs.get("user_id") is None


@pytest.mark.asyncio
async def test_poll_now_response_shape(_app):
    """POST /api/zotero/poll response conforms to JobEnqueuedResponse schema."""
    app, _conn = _app

    with patch(
        "jarvis_common.task_registry.zotero_sync_from_zotero.defer_async",
        new=AsyncMock(return_value=None),
    ):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/zotero/poll",
                headers={"X-API-Key": "test"},
            )

    body = resp.json()
    assert set(body.keys()) >= {"job_id", "status"}
    assert isinstance(body["job_id"], str)
    assert isinstance(body["status"], str)
