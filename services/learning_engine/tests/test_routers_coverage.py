"""Router coverage tests for learning_engine.

Covers endpoints not exercised in test_le_endpoints, test_executive, or
test_le_hardening:

- GET  /api/projects          — list projects (happy path)
- POST /api/projects          — create project
- GET  /api/projects/{id}     — project detail
- DELETE /api/projects/{id}   — delete project
- GET  /api/projects/{id}/tasks   — list tasks for project
- POST /api/projects/{id}/tasks   — create task
- PUT  /api/tasks/{id}            — update task
- GET  /api/projects/{id}/milestones  — list milestones
- POST /api/projects/{id}/milestones  — create milestone
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from httpx import ASGITransport

from tests.conftest import FakeRecord, _make_pool_and_conn

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


def _make_project_row(id=1, name="TestProject", status="active"):
    return FakeRecord(
        id=id,
        name=name,
        description=None,
        status=status,
        deadline=None,
        color=None,
        created_at=_now(),
        updated_at=_now(),
    )


def _make_task_row(**overrides):
    row = {
        "id": 1,
        "project_id": 1,
        "parent_task_id": None,
        "title": "Do stuff",
        "description": None,
        "status": "todo",
        "priority": 3,
        "deadline": None,
        "estimated_hours": None,
        "actual_hours": None,
        "sort_order": 0,
        "completed_at": None,
        "created_at": _now(),
        "updated_at": _now(),
    }
    row.update(overrides)
    return FakeRecord(**row)


def _make_milestone_row(**overrides):
    row = {
        "id": 1,
        "project_id": 1,
        "name": "M1",
        "deadline": date.today() + timedelta(days=7),
        "description": None,
        "completed": False,
        "completed_at": None,
        "created_at": _now(),
    }
    row.update(overrides)
    return FakeRecord(**row)


# ---------------------------------------------------------------------------
# Shared app fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def _app():
    """Minimal app fixture with mocked DB, disabled auth + rate-limiter."""
    from jarvis_common import verify_api_key
    from jarvis_common.auth import (
        current_user_id_strict,
        current_user_id_strict_with_owner_override,
    )
    from learning_engine.deps import (
        get_anki_exporter,
        get_card_generator,
        get_db_pool,
        get_fsrs_manager,
    )
    from learning_engine.main import app

    mock_pool, conn = _make_pool_and_conn()
    app.state.db_pool = mock_pool
    app.state.limiter.enabled = False

    mock_http = AsyncMock()
    app.state.http_client = mock_http

    mock_fsrs = MagicMock()
    mock_fsrs.create_new_card.return_value = ({}, _now())
    mock_fsrs.schedule_review.return_value = ({}, {}, _now() + timedelta(days=1))
    app.state.fsrs_manager = mock_fsrs

    mock_generator = AsyncMock()
    app.state.card_generator = mock_generator

    mock_exporter = MagicMock()
    app.state.anki_exporter = mock_exporter

    app.dependency_overrides[get_db_pool] = lambda: mock_pool
    app.dependency_overrides[verify_api_key] = lambda: None
    app.dependency_overrides[get_fsrs_manager] = lambda: mock_fsrs
    app.dependency_overrides[get_card_generator] = lambda: mock_generator
    app.dependency_overrides[get_anki_exporter] = lambda: mock_exporter
    app.dependency_overrides[current_user_id_strict] = lambda: 1
    app.dependency_overrides[current_user_id_strict_with_owner_override] = lambda: 1

    yield app, conn, mock_pool
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# GET /api/projects
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_projects_returns_list(_app):
    """GET /api/projects returns a JSON list of projects."""
    app, conn, pool = _app
    conn.fetch = AsyncMock(
        return_value=[_make_project_row(id=1, name="Alpha"), _make_project_row(id=2, name="Beta")]
    )

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/api/projects")

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2
    assert body[0]["name"] == "Alpha"
    assert body[1]["name"] == "Beta"


# ---------------------------------------------------------------------------
# POST /api/projects
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_project_returns_201(_app):
    """POST /api/projects creates a project and returns 201."""
    app, conn, pool = _app
    pool.fetchrow = AsyncMock(return_value=_make_project_row(id=5, name="NewProj"))

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/api/projects", json={"name": "NewProj"})

    assert resp.status_code == 201
    body = resp.json()
    assert body["id"] == 5
    assert body["name"] == "NewProj"


@pytest.mark.asyncio
async def test_create_project_invalid_color_returns_422(_app):
    """POST /api/projects with an invalid color returns 422 validation error."""
    app, *_ = _app

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/api/projects", json={"name": "Valid Name", "color": "notacolor"})

    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/projects/{project_id} (detail)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_project_detail_not_found(_app):
    """GET /api/projects/999 returns 404 when project does not exist."""
    app, conn, pool = _app
    conn.fetchrow.return_value = None
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/api/projects/999")

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /api/projects/{project_id}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_project_returns_204(_app):
    """DELETE /api/projects/{id} returns 204 when project exists.

    The real ``delete_or_404`` helper runs; it calls ``pool.execute(DELETE ...)``
    and checks the result tag.  Returning ``"DELETE 1"`` means a row was found and
    deleted, so no 404 is raised.  This replaces the previous ``patch.object``
    over-mock that bypassed all business logic.
    """
    app, conn, pool = _app
    # delete_or_404 calls pool.execute(DELETE ...) and checks result != "DELETE 0".
    pool.execute = AsyncMock(return_value="DELETE 1")

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.delete("/api/projects/1")

    assert resp.status_code == 204


# ---------------------------------------------------------------------------
# GET /api/projects/{project_id}/tasks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_tasks_for_project(_app):
    """GET /api/projects/1/tasks returns tasks belonging to that project."""
    app, conn, pool = _app
    conn.fetchval.return_value = 1  # project exists
    conn.fetch.return_value = [
        _make_task_row(id=10, project_id=1, title="Task A"),
        _make_task_row(id=11, project_id=1, title="Task B"),
    ]

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/api/projects/1/tasks")

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2
    assert body[0]["title"] == "Task A"


@pytest.mark.asyncio
async def test_list_tasks_project_not_found(_app):
    """GET /api/projects/999/tasks returns 404 when project does not exist."""
    app, conn, pool = _app
    conn.fetchval.return_value = None  # project missing

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/api/projects/999/tasks")

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/projects/{project_id}/tasks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_task_returns_201(_app):
    """POST /api/projects/1/tasks creates a task and returns 201."""
    app, conn, pool = _app
    conn.fetchval.return_value = 1  # project exists
    conn.fetchrow.return_value = _make_task_row(id=20, project_id=1, title="New task")

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/api/projects/1/tasks", json={"title": "New task"})

    assert resp.status_code == 201
    body = resp.json()
    assert body["id"] == 20
    assert body["title"] == "New task"


# ---------------------------------------------------------------------------
# PUT /api/tasks/{task_id}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_task_not_found(_app):
    """PUT /api/tasks/999 returns 404 when the task does not exist.

    The real ``update_task`` handler runs the full DB path: it opens a transaction,
    issues ``SELECT * FROM tasks … FOR UPDATE`` (via ``conn.fetchrow``), and raises 404
    immediately when ``fetchrow`` returns ``None``.  Setting ``conn.fetchrow.return_value
    = None`` simulates the missing-row case without bypassing any business logic.
    """
    app, conn, pool = _app
    # fetchrow(SELECT … FOR UPDATE) returns None → task not found → 404.
    conn.fetchrow.return_value = None

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.put("/api/tasks/999", json={"title": "Updated"})

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/projects/{project_id}/milestones
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_milestones_for_project(_app):
    """GET /api/projects/1/milestones returns milestones for that project."""
    app, conn, pool = _app
    conn.fetchval.return_value = 1  # project exists
    conn.fetch.return_value = [_make_milestone_row(id=30, project_id=1, name="M1")]

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/api/projects/1/milestones")

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["name"] == "M1"


@pytest.mark.asyncio
async def test_list_milestones_project_not_found(_app):
    """GET /api/projects/999/milestones returns 404 when project does not exist."""
    app, conn, pool = _app
    conn.fetchval.return_value = None  # project missing

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/api/projects/999/milestones")

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/projects/{project_id}/milestones
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_milestone_returns_201(_app):
    """POST /api/projects/1/milestones creates a milestone and returns 201."""
    app, conn, pool = _app
    conn.fetchval.return_value = 1  # project exists
    conn.fetchrow.return_value = _make_milestone_row(id=50, project_id=1, name="Submit paper")

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(
            "/api/projects/1/milestones",
            json={"name": "Submit paper", "deadline": "2026-06-01T12:00:00+00:00"},
        )

    assert resp.status_code == 201
    body = resp.json()
    assert body["id"] == 50
    assert body["name"] == "Submit paper"
