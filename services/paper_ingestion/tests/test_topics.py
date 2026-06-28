"""Auth gate unit tests for the topics list endpoint.

GET /api/topics must require a signed-in session; any API-key-only caller
(no session cookie) must receive 401.  Sibling sub-routes already gate via
``current_user_id_strict``; this file covers the list endpoint that was
missing the same gate.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from httpx import ASGITransport

from tests.conftest import FakeRecord, _make_pool_and_conn


def _topic_row(id: int = 1, name: str = "ML Topics") -> FakeRecord:
    return FakeRecord(
        id=id,
        name=name,
        query_terms=["machine learning"],
        category=None,
        description=None,
        enabled=True,
        created_at=datetime.now(UTC),
    )


def _client(app: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture()
def _app(request: pytest.FixtureRequest):
    """App with mocked DB, bypassed API-key auth, limiter off.

    ``request.param``:
    - ``None``    — API-key-only caller, no session (auth gate must reject).
    - ``"user"``  — authenticated user session (auth gate must pass).
    """
    from jarvis_common import verify_api_key
    from jarvis_common.auth import current_user_id_strict
    from paper_ingestion.deps import get_db_pool
    from paper_ingestion.main import app

    user_role = getattr(request, "param", None)

    mock_pool, conn = _make_pool_and_conn()
    conn.fetch.return_value = [_topic_row()]

    app.state.db_pool = mock_pool
    app.state.limiter.enabled = False
    app.dependency_overrides[get_db_pool] = lambda: mock_pool
    app.dependency_overrides[verify_api_key] = lambda: None

    if user_role is not None:
        app.dependency_overrides[current_user_id_strict] = lambda: 1

    yield app, conn

    app.dependency_overrides.clear()
    app.state.limiter.enabled = True


# ---------------------------------------------------------------------------
# list_topics: GET /api/topics — requires a signed-in session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("_app", [None], indirect=True)
async def test_list_topics_requires_session(_app: tuple) -> None:
    """GET /api/topics without a session cookie returns 401."""
    app, _conn = _app
    async with _client(app) as c:
        resp = await c.get("/api/topics")
    assert resp.status_code == 401, resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize("_app", ["user"], indirect=True)
async def test_list_topics_returns_list_with_valid_session(_app: tuple) -> None:
    """GET /api/topics with a valid session returns 200 and a list."""
    app, _conn = _app
    async with _client(app) as c:
        resp = await c.get("/api/topics")
    assert resp.status_code == 200, resp.text
    assert isinstance(resp.json(), list)
