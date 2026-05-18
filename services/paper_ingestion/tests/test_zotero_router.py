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

from tests.conftest import _make_pool_and_conn

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


@pytest.mark.asyncio
async def test_test_zotero_connection_reads_current_user_config(monkeypatch):
    """Credential tests should prefer the browser user's Zotero config row."""
    from paper_ingestion.routers import zotero

    monkeypatch.setattr(zotero, "current_user_id_strict", AsyncMock(return_value=42))
    get_config = AsyncMock(
        return_value={"api_key": "key", "user_id": "123", "library_type": "user"}
    )
    monkeypatch.setattr(
        "paper_ingestion.integrations.zotero_service._get_zotero_config",
        get_config,
    )
    mock_client = MagicMock()
    mock_client.return_value.test_connection = AsyncMock(return_value=True)

    with patch("paper_ingestion.integrations.zotero_client.ZoteroClient", mock_client):
        pool = MagicMock()
        result = await zotero.test_zotero_connection.__wrapped__(
            MagicMock(),
            db_pool=pool,
            http_client=AsyncMock(spec=httpx.AsyncClient),
        )

    assert result == {"ok": True}
    get_config.assert_awaited_once_with(pool, user_id=42)


@pytest.mark.asyncio
async def test_get_paper_zotero_state_checks_ownership(monkeypatch):
    """Zotero state reads expose paper-specific metadata and must enforce ownership."""
    from fastapi import HTTPException
    from paper_ingestion.routers import zotero

    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = {
        "zotero_item_key": "ITEM",
        "zotero_citation_key": "Smith2026",
        "zotero_last_pushed_at": None,
    }
    monkeypatch.setattr(zotero, "current_user_id_strict", AsyncMock(return_value=42))
    deny = HTTPException(status_code=403, detail="paper not owned by current user")
    ownership = AsyncMock(side_effect=deny)
    monkeypatch.setattr(zotero, "assert_paper_ownership", ownership)

    with pytest.raises(HTTPException) as exc_info:
        await zotero.get_paper_zotero_state.__wrapped__(
            MagicMock(),
            paper_id=7,
            db_pool=pool,
        )

    assert exc_info.value.status_code == 403
    ownership.assert_awaited_once_with(conn, 7, 42)


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
    import jarvis_common.task_registry as task_registry

    app, _conn = _app

    mock_task = MagicMock()
    mock_defer = AsyncMock(return_value=None)
    mock_task.defer_async = mock_defer
    with patch.dict(task_registry._TASK_MAP, {"zotero.sync_from_zotero": mock_task}):
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
    assert call_kwargs.get("user_id") == 1  # real authenticated user (no NULL-owned jobs)


@pytest.mark.asyncio
async def test_poll_now_response_shape(_app):
    """POST /api/zotero/poll response conforms to JobEnqueuedResponse schema."""
    import jarvis_common.task_registry as task_registry

    app, _conn = _app

    mock_task = MagicMock()
    mock_task.defer_async = AsyncMock(return_value=None)
    with patch.dict(task_registry._TASK_MAP, {"zotero.sync_from_zotero": mock_task}):
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
