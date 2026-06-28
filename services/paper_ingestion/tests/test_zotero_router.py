"""Zotero router tests.

Covers:
- POST /api/zotero/poll enqueues a zotero.sync_from_zotero job and
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


@pytest.mark.real_auth
@pytest.mark.asyncio
async def test_test_zotero_connection_reads_current_user_config(monkeypatch):
    """Credential tests should prefer the browser user's Zotero config row.

    Drives POST /api/zotero/test through the full router stack via the ASGI
    client. ``current_user_id_strict`` is resolved by FastAPI ``Depends`` so it
    is steered through ``app.dependency_overrides`` (not a module monkeypatch).
    """
    from jarvis_common import verify_api_key
    from jarvis_common.auth import current_user_id_strict
    from paper_ingestion.deps import get_db_pool, get_http_client
    from paper_ingestion.main import app

    pool = MagicMock()
    http_client = AsyncMock(spec=httpx.AsyncClient)
    get_config = AsyncMock(
        return_value={"api_key": "key", "user_id": "123", "library_type": "user"}
    )
    monkeypatch.setattr(
        "paper_ingestion.integrations.zotero_service._get_zotero_config",
        get_config,
    )
    mock_client = MagicMock()
    mock_client.return_value.test_connection = AsyncMock(return_value=True)

    app.state.limiter.enabled = False
    app.dependency_overrides[get_db_pool] = lambda: pool
    app.dependency_overrides[get_http_client] = lambda: http_client
    app.dependency_overrides[verify_api_key] = lambda: None
    app.dependency_overrides[current_user_id_strict] = lambda: 42
    try:
        with patch("paper_ingestion.integrations.zotero_client.ZoteroClient", mock_client):
            async with httpx.AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post("/api/zotero/test", headers={"X-API-Key": "test"})
    finally:
        app.dependency_overrides.clear()
        app.state.limiter.enabled = True

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True}
    get_config.assert_awaited_once_with(pool, user_id=42)


@pytest.mark.real_auth
@pytest.mark.asyncio
async def test_get_paper_zotero_state_checks_ownership(monkeypatch):
    """Zotero state reads expose paper-specific metadata and must enforce ownership.

    Drives GET /api/papers/{id}/zotero through the full router stack; the
    ownership guard is stubbed to deny, and ``current_user_id_strict`` is steered
    via ``app.dependency_overrides`` (the route resolves it through ``Depends``).
    """
    from fastapi import HTTPException
    from jarvis_common import verify_api_key
    from jarvis_common.auth import current_user_id_strict
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app
    from paper_ingestion.routers import zotero

    pool, conn = _make_pool_and_conn()
    conn.fetchrow.return_value = {
        "zotero_item_key": "ITEM",
        "zotero_citation_key": "Smith2026",
        "zotero_last_pushed_at": None,
    }
    deny = HTTPException(status_code=403, detail="paper not owned by current user")
    ownership = AsyncMock(side_effect=deny)
    monkeypatch.setattr(zotero, "assert_paper_ownership", ownership)

    app.state.limiter.enabled = False
    app.dependency_overrides[get_db_pool] = lambda: pool
    app.dependency_overrides[verify_api_key] = lambda: None
    app.dependency_overrides[current_user_id_strict] = lambda: 42
    try:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/papers/7/zotero", headers={"X-API-Key": "test"})
    finally:
        app.dependency_overrides.clear()
        app.state.limiter.enabled = True

    assert resp.status_code == 403
    ownership.assert_awaited_once_with(conn, 7, 42)


@pytest.mark.real_auth
@pytest.mark.asyncio
async def test_get_paper_zotero_state_is_per_user(monkeypatch):
    """Zotero state is scoped to the requesting user's link row, not a global column.

    User 1 pushed paper 7 (a ``paper_user_zotero_links`` row exists for them);
    user 2 has no link for the same paper. User 1 must see their own item key and
    user 2 must see the un-pushed (NULL) state — the pre-0101 read of the shared
    ``papers.zotero_*`` columns would leak user 1's key to user 2.
    """
    # Verified: routers/zotero.py:147 — fetchrow keys the link read on $2 = user_id.
    from jarvis_common import verify_api_key
    from jarvis_common.auth import current_user_id_strict
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app
    from paper_ingestion.routers import zotero

    pool, conn = _make_pool_and_conn()
    monkeypatch.setattr(zotero, "assert_paper_ownership", AsyncMock())

    pushed_row = {
        "zotero_item_key": "ITEM-USER1",
        "zotero_citation_key": "Smith2026",
        "zotero_last_pushed_at": None,
    }
    unpushed_row = {
        "zotero_item_key": None,
        "zotero_citation_key": None,
        "zotero_last_pushed_at": None,
    }

    def _link_row_for_user(_query, paper_id, user_id):
        return pushed_row if user_id == 1 else unpushed_row

    conn.fetchrow = AsyncMock(side_effect=_link_row_for_user)

    app.state.limiter.enabled = False
    app.dependency_overrides[get_db_pool] = lambda: pool
    app.dependency_overrides[verify_api_key] = lambda: None
    try:
        app.dependency_overrides[current_user_id_strict] = lambda: 1
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp_owner = await client.get("/api/papers/7/zotero", headers={"X-API-Key": "test"})

        app.dependency_overrides[current_user_id_strict] = lambda: 2
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp_other = await client.get("/api/papers/7/zotero", headers={"X-API-Key": "test"})
    finally:
        app.dependency_overrides.clear()
        app.state.limiter.enabled = True

    assert resp_owner.status_code == 200, resp_owner.text
    assert resp_owner.json()["zotero_item_key"] == "ITEM-USER1"
    assert resp_other.status_code == 200, resp_other.text
    assert resp_other.json()["zotero_item_key"] is None


# ---------------------------------------------------------------------------
# POST /api/zotero/poll
# ---------------------------------------------------------------------------


@pytest.fixture()
def _app():
    """FastAPI app with mocked DB pool, bypassed API-key auth, and disabled rate limiter.

    ``current_user_id_strict`` (resolved via ``Depends`` on the zotero routes) is
    steered to user 1 through ``app.dependency_overrides`` — the same identity the
    legacy in-body resolver produced for these single-tenant tests.
    """
    from jarvis_common import verify_api_key
    from jarvis_common.auth import current_user_id_strict
    from paper_ingestion.deps import get_db_pool, get_http_client
    from paper_ingestion.main import app

    mock_pool, conn = _make_pool_and_conn()
    app.state.db_pool = mock_pool
    app.state.limiter.enabled = False

    app.dependency_overrides[get_db_pool] = lambda: mock_pool
    app.dependency_overrides[get_http_client] = lambda: AsyncMock(spec=httpx.AsyncClient)
    app.dependency_overrides[verify_api_key] = lambda: None
    app.dependency_overrides[current_user_id_strict] = lambda: 1
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


# ---------------------------------------------------------------------------
# ZoteroConfigDecryptError — test_zotero_connection router handler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_test_zotero_connection_returns_user_visible_detail_on_decrypt_error(
    monkeypatch, caplog, _app
):
    """test_zotero_connection returns {ok: False, detail: ...} and warns on decrypt error.

    Uses the FastAPI test client via dependency_overrides so the full router
    stack (rate-limiter, exception handling) is exercised. ``current_user_id_strict``
    is resolved via ``Depends`` and steered to user 1 by the ``_app`` fixture.
    """
    import logging

    from paper_ingestion.integrations.zotero_service import ZoteroConfigDecryptError

    app, _conn = _app

    monkeypatch.setattr(
        "paper_ingestion.integrations.zotero_service._get_zotero_config",
        AsyncMock(side_effect=ZoteroConfigDecryptError("api_key")),
    )

    with caplog.at_level(logging.WARNING, logger="paper_ingestion.routers.zotero"):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/zotero/test",
                headers={"X-API-Key": "test"},
            )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert "detail" in body
    assert any("unreadable" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# POST /api/zotero/push-highlights/{paper_id} — view-level export authz
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_push_highlights_allows_visible_not_owned_public_paper(_app):
    """A public-source (arXiv) paper discovered by another user and not in the
    caller's library is now exportable (202) — view-level authz matches the
    create/list-highlights router (view => annotate => export). Previously this
    enforced ownership and returned 403."""
    import jarvis_common.task_registry as task_registry
    from tests.conftest import FakeRecord

    app, conn = _app
    # Public-source paper, foreign discoverer, absent from caller's library →
    # visible to any authenticated user under assert_paper_pdf_visible.
    conn.fetchrow.return_value = FakeRecord(
        {"source_type": "arxiv", "discovered_by": 999, "in_library": False}
    )

    mock_task = MagicMock()
    mock_task.defer_async = AsyncMock(return_value=None)
    with patch.dict(task_registry._TASK_MAP, {"zotero.push_highlights": mock_task}):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post("/api/zotero/push-highlights/7", headers={"X-API-Key": "test"})

    assert resp.status_code == 202, resp.text
    assert resp.json()["status"] == "queued"
    mock_task.defer_async.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/api/papers/7/zotero"),
        ("get", "/api/papers/7/zotero"),
        ("post", "/api/zotero/resync/7"),
        ("post", "/api/zotero/sync-annotations/7"),
    ],
)
async def test_ownership_endpoints_still_403_on_foreign_public_paper(_app, method, path):
    """Regression: the four ownership-gated Zotero endpoints keep the stricter
    ownership check. A public-source paper owned by another user and not in the
    caller's library is still 403, even though it is now exportable via
    push-highlights — only highlight export was loosened to view level."""
    from tests.conftest import FakeRecord

    app, conn = _app
    # Foreign paper: discovered by another user (999), absent from the caller's
    # library → ownership denied. fetchval answers the existence probe (truthy)
    # and the library-membership probe (None) by inspecting the query.
    conn.fetchrow.return_value = FakeRecord({"discovered_by": 999})
    conn.fetchval.side_effect = lambda query, *a: None if "user_library" in query else 7

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.request(method, path, headers={"X-API-Key": "test"})

    assert resp.status_code == 403, resp.text
