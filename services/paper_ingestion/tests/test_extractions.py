"""RBAC tests for extraction-template CUD endpoints (PI-B).

``extraction_templates`` is an instance-global table (no ``user_id`` column),
so create/update/delete must be admin-only. Any authenticated non-admin tenant
mutating it would break extraction for every user (e.g. deleting the default
template).

- non-admin session  → 403 on each CUD op
- admin session      → success
- read endpoint (GET /api/extraction-templates) is unaffected
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from fastapi import HTTPException
from httpx import ASGITransport

from tests.conftest import FakeRecord, _make_pool_and_conn


def _template_row(id=1, name="Default Template", is_default=True):
    return FakeRecord(
        id=id,
        name=name,
        description=None,
        fields=[{"name": "method", "label": "Method", "description": "d", "type": "text"}],
        is_default=is_default,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def _client(app):
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture()
def _app(request):
    """Full app with mocked DB, bypassed API-key auth, limiter off.

    ``request.param`` (parametrized via indirect) is the simulated session
    role: ``None``/``"user"`` exercise the rejection path (the real
    ``require_admin`` raises 403 when ``request.state.user_role != 'admin'``),
    ``"admin"`` exercises the success path.

    The ``require_admin`` override resolves the role via the live ASGI request
    rather than a FastAPI-injected ``Request`` dependency: an injected
    ``Request`` param on the override callable is misresolved by FastAPI as a
    query parameter once the slowapi-wrapped endpoint also declares a body.
    """
    from jarvis_common import verify_api_key
    from jarvis_common.auth import require_admin
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app

    user_role = getattr(request, "param", None)

    mock_pool, conn = _make_pool_and_conn()
    conn.fetch.return_value = [_template_row()]
    conn.fetchrow.return_value = _template_row(id=3, name="New Template")
    conn.execute.return_value = "DELETE 1"

    app.state.db_pool = mock_pool
    app.state.limiter.enabled = False
    app.dependency_overrides[get_db_pool] = lambda: mock_pool
    app.dependency_overrides[verify_api_key] = lambda: None

    async def _patched_require_admin() -> None:
        if user_role != "admin":
            raise HTTPException(status_code=403, detail="Admin role required")

    app.dependency_overrides[require_admin] = _patched_require_admin

    yield app, conn

    app.dependency_overrides.clear()
    app.state.limiter.enabled = True


_VALID_BODY = {
    "name": "T",
    "fields": [{"name": "f", "label": "F", "description": "d", "type": "text"}],
}


# ---------------------------------------------------------------------------
# Non-admin → 403 on every CUD op
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("_app", ["user"], indirect=True)
async def test_create_template_rejects_non_admin(_app):
    app, conn = _app
    async with _client(app) as c:
        resp = await c.post("/api/extraction-templates", json=_VALID_BODY)
    assert resp.status_code == 403, resp.text
    conn.fetchrow.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("_app", ["user"], indirect=True)
async def test_update_template_rejects_non_admin(_app):
    app, conn = _app
    async with _client(app) as c:
        resp = await c.put("/api/extraction-templates/1", json={"name": "X"})
    assert resp.status_code == 403, resp.text
    conn.fetchrow.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("_app", ["user"], indirect=True)
async def test_delete_template_rejects_non_admin(_app):
    app, conn = _app
    async with _client(app) as c:
        resp = await c.delete("/api/extraction-templates/1")
    assert resp.status_code == 403, resp.text
    conn.execute.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("_app", [None], indirect=True)
async def test_create_template_rejects_no_session(_app):
    """API-key-only caller (no session ⇒ no user_role) is not an admin."""
    app, conn = _app
    async with _client(app) as c:
        resp = await c.post("/api/extraction-templates", json=_VALID_BODY)
    assert resp.status_code == 403, resp.text
    conn.fetchrow.assert_not_called()


# ---------------------------------------------------------------------------
# Admin → success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("_app", ["admin"], indirect=True)
async def test_create_template_allows_admin(_app):
    app, conn = _app
    async with _client(app) as c:
        resp = await c.post("/api/extraction-templates", json=_VALID_BODY)
    assert resp.status_code == 201, resp.text
    assert resp.json()["name"] == "New Template"


@pytest.mark.asyncio
@pytest.mark.parametrize("_app", ["admin"], indirect=True)
async def test_delete_template_allows_admin(_app):
    app, conn = _app
    async with _client(app) as c:
        resp = await c.delete("/api/extraction-templates/1")
    assert resp.status_code == 204, resp.text
    conn.execute.assert_called_once()


# ---------------------------------------------------------------------------
# Read endpoint is unaffected by the admin gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("_app", [None, "user"], indirect=True)
async def test_list_templates_unaffected_by_admin_gate(_app):
    app, conn = _app
    async with _client(app) as c:
        resp = await c.get("/api/extraction-templates")
    assert resp.status_code == 200, resp.text
    assert len(resp.json()) == 1
