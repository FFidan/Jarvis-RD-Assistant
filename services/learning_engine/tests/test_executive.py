"""Tests for executive endpoints in the Learning Engine service.

Covers:
- GET  /api/executive/my-day  — tasks, cards, recs, focus stats, pulse
- POST /api/executive/tasks   — quick-add a task (standalone, with project, invalid project)
- POST /api/executive/focus/log — logs a focus session (bare timer, task, paper, not-found)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from httpx import ASGITransport

# ---------------------------------------------------------------------------
# Helpers (mirrors conftest + test_le_endpoints patterns)
# ---------------------------------------------------------------------------


class FakeRecord(dict):
    """Dict subclass that behaves like an asyncpg.Record."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def get(self, key, default=None):
        return super().get(key, default)

    def keys(self):
        return super().keys()

    def values(self):
        return super().values()


def _make_pool_and_conn():
    """Create a mock asyncpg Pool whose acquire() returns an async CM."""
    conn = AsyncMock()

    txn_cm = MagicMock()
    txn_cm.__aenter__ = AsyncMock(return_value=txn_cm)
    txn_cm.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn_cm)

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)

    pool = MagicMock()
    pool.acquire.return_value = ctx
    return pool, conn


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def exec_app():
    """Minimal app with mocked dependencies and disabled auth + rate limiting."""
    from jarvis_common import verify_api_key
    from learning_engine.deps import get_db_pool
    from learning_engine.main import app

    mock_pool, conn = _make_pool_and_conn()
    app.state.db_pool = mock_pool
    app.state.limiter.enabled = False

    app.dependency_overrides[get_db_pool] = lambda: mock_pool
    app.dependency_overrides[verify_api_key] = lambda: None

    yield app, conn

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Tests: GET /api/executive/my-day
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_my_day_happy_path(exec_app):
    """GET /api/executive/my-day returns tasks, cards_due, and recommendations."""
    app, conn = exec_app

    task_rows = [
        FakeRecord(
            id=1,
            project_id=None,
            title="Write report",
            priority=1,
            deadline=None,
            status="todo",
            completed_at=None,
            project_name=None,
            project_color=None,
        ),
        FakeRecord(
            id=2,
            project_id=10,
            title="Review PR",
            priority=2,
            deadline=None,
            status="todo",
            completed_at=None,
            project_name="JARVIS",
            project_color="#3b82f6",
        ),
        FakeRecord(
            id=3,
            project_id=None,
            title="Fix bug",
            priority=3,
            deadline=None,
            status="todo",
            completed_at=None,
            project_name=None,
            project_color=None,
        ),
    ]
    rec_rows = [
        FakeRecord(
            recommendation_id=1, paper_id=100, score=0.95, title="Neural ODEs", authors=["Chen"]
        ),
        FakeRecord(
            recommendation_id=2,
            paper_id=101,
            score=0.88,
            title="Attention is All You Need",
            authors=["Vaswani"],
        ),
    ]

    # fetch is called 4 times: tasks, recommendations, streak_rows, project_pulse
    conn.fetch.side_effect = [task_rows, rec_rows, [], []]
    # fetchval is called 2 times: cards_due, today_focus_hours
    conn.fetchval.side_effect = [5, 2.5]

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/executive/my-day")

    assert resp.status_code == 200
    data = resp.json()

    assert "tasks" in data
    assert "cards_due" in data
    assert "recommendations" in data

    assert len(data["tasks"]) == 3
    assert data["tasks"][0]["title"] == "Write report"
    assert data["cards_due"] == 5
    assert len(data["recommendations"]) == 2
    assert data["recommendations"][0]["title"] == "Neural ODEs"


@pytest.mark.asyncio
async def test_my_day_empty(exec_app):
    """GET /api/executive/my-day returns empty lists and zero when nothing is due."""
    app, conn = exec_app

    conn.fetch.side_effect = [[], [], [], []]
    conn.fetchval.side_effect = [0, 0]

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/executive/my-day")

    assert resp.status_code == 200
    data = resp.json()
    assert data["tasks"] == []
    assert data["cards_due"] == 0
    assert data["recommendations"] == []
    assert data["today_focus_hours"] == 0
    assert data["focus_streak_days"] == 0
    assert data["project_pulse"] == []


@pytest.mark.asyncio
async def test_my_day_limit_recommendations_query_param(exec_app):
    """GET /api/executive/my-day?limit_recommendations=1 passes limit to SQL."""
    app, conn = exec_app

    rec_row = FakeRecord(
        recommendation_id=1, paper_id=100, score=0.9, title="Paper A", authors=["Author"]
    )
    conn.fetch.side_effect = [[], [rec_row], [], []]
    conn.fetchval.side_effect = [0, 0]

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/executive/my-day", params={"limit_recommendations": 1})

    assert resp.status_code == 200
    # Verify the query param was actually passed to the SQL call (second fetch call)
    rec_fetch_call = conn.fetch.call_args_list[1]
    assert rec_fetch_call[0][1] == 1  # positional arg $1 = limit_recommendations


# ---------------------------------------------------------------------------
# Tests: POST /api/executive/focus/log
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_focus_log_bare_timer(exec_app):
    """POST /api/executive/focus/log with only duration_hours returns 200."""
    app, conn = exec_app

    conn.execute.return_value = "INSERT 1"

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/executive/focus/log",
            json={"duration_hours": 1.5},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert data["recorded_hours"] == 1.5


@pytest.mark.asyncio
async def test_focus_log_with_task_id(exec_app):
    """POST /api/executive/focus/log with task_id returns 200 and calls execute."""
    app, conn = exec_app

    # UPDATE tasks returns "UPDATE 1" (task found and updated)
    conn.execute.return_value = "UPDATE 1"

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/executive/focus/log",
            json={"duration_hours": 2.0, "task_id": 42},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert data["recorded_hours"] == 2.0

    # execute should have been called at least twice:
    # once for UPDATE tasks, once for INSERT daily_log
    assert conn.execute.call_count >= 2

    # Verify the task UPDATE was attempted
    first_execute_sql = conn.execute.call_args_list[0][0][0]
    assert "tasks" in first_execute_sql.lower()


@pytest.mark.asyncio
async def test_focus_log_task_not_found(exec_app):
    """POST /api/executive/focus/log with a missing task_id returns 404."""
    app, conn = exec_app

    # fetchrow (SELECT FOR UPDATE) returns None — task doesn't exist
    conn.fetchrow.return_value = None

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/executive/focus/log",
            json={"duration_hours": 1.0, "task_id": 99999},
        )

    assert resp.status_code == 404
    assert "task" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_focus_log_with_paper_id(exec_app):
    """POST /api/executive/focus/log with paper_id returns 200."""
    app, conn = exec_app

    # fetchrow (SELECT FOR UPDATE) returns a row — paper exists
    conn.fetchrow.return_value = FakeRecord(id=7)
    conn.execute.return_value = "INSERT 1"

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/executive/focus/log",
            json={"duration_hours": 0.5, "paper_id": 7},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert data["recorded_hours"] == 0.5


@pytest.mark.asyncio
async def test_focus_log_paper_not_found(exec_app):
    """POST /api/executive/focus/log with a missing paper_id returns 404."""
    app, conn = exec_app

    # fetchrow (SELECT FOR UPDATE) returns None — paper does not exist
    conn.fetchrow.return_value = None

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/executive/focus/log",
            json={"duration_hours": 1.0, "paper_id": 99999},
        )

    assert resp.status_code == 404
    assert "paper" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_focus_log_missing_duration_returns_422(exec_app):
    """POST /api/executive/focus/log without duration_hours returns 422."""
    app, _ = exec_app

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/executive/focus/log",
            json={"task_id": 1},
        )

    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Tests: GET /api/executive/my-day (expanded response fields)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_my_day_tasks_include_project_context(exec_app):
    """Tasks include project_name and project_color from LEFT JOIN."""
    app, conn = exec_app
    task_rows = [
        FakeRecord(
            id=1,
            project_id=10,
            title="Task A",
            priority=1,
            deadline=None,
            status="todo",
            completed_at=None,
            project_name="JARVIS",
            project_color="#3b82f6",
        ),
        FakeRecord(
            id=2,
            project_id=None,
            title="Standalone",
            priority=2,
            deadline=None,
            status="todo",
            completed_at=None,
            project_name=None,
            project_color=None,
        ),
    ]
    conn.fetch.side_effect = [task_rows, [], [], []]
    conn.fetchval.side_effect = [0, 0]

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/executive/my-day")

    assert resp.status_code == 200
    tasks = resp.json()["tasks"]
    assert tasks[0]["project_name"] == "JARVIS"
    assert tasks[0]["project_color"] == "#3b82f6"
    assert tasks[1]["project_name"] is None


@pytest.mark.asyncio
async def test_my_day_returns_focus_stats(exec_app):
    """Response includes today_focus_hours and focus_streak_days."""
    import datetime

    app, conn = exec_app
    # Use UTC date to match the streak logic in executive.py which calls
    # datetime.datetime.now(datetime.UTC).date() — local and UTC may differ.
    today = datetime.datetime.now(datetime.UTC).date()
    streak_rows = [
        FakeRecord(log_date=today),
        FakeRecord(log_date=today - datetime.timedelta(days=1)),
        FakeRecord(log_date=today - datetime.timedelta(days=2)),
    ]
    conn.fetch.side_effect = [[], [], streak_rows, []]
    conn.fetchval.side_effect = [0, 1.5]

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/executive/my-day")

    assert resp.status_code == 200
    data = resp.json()
    assert data["today_focus_hours"] == 1.5
    assert data["focus_streak_days"] == 3


@pytest.mark.asyncio
async def test_my_day_returns_project_pulse(exec_app):
    """Response includes project_pulse with progress data."""
    app, conn = exec_app
    pulse_rows = [
        FakeRecord(
            id=1,
            name="JARVIS",
            total_tasks=10,
            done_tasks=7,
            next_milestone="v2 release",
            next_milestone_deadline=None,
        ),
    ]
    conn.fetch.side_effect = [[], [], [], pulse_rows]
    conn.fetchval.side_effect = [0, 0]

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/executive/my-day")

    assert resp.status_code == 200
    pulse = resp.json()["project_pulse"]
    assert len(pulse) == 1
    assert pulse[0]["name"] == "JARVIS"
    assert pulse[0]["done_tasks"] == 7


@pytest.mark.asyncio
async def test_my_day_includes_completed_tasks_today(exec_app):
    """GET /api/executive/my-day includes done tasks from today alongside pending ones."""
    app, conn = exec_app

    task_rows = [
        FakeRecord(
            id=1,
            project_id=None,
            title="Pending task",
            priority=1,
            deadline=None,
            status="todo",
            completed_at=None,
            project_name=None,
            project_color=None,
        ),
        FakeRecord(
            id=2,
            project_id=None,
            title="Done today",
            priority=2,
            deadline=None,
            status="done",
            completed_at="2026-04-05T14:00:00+00:00",
            project_name=None,
            project_color=None,
        ),
    ]
    conn.fetch.side_effect = [task_rows, [], [], []]
    conn.fetchval.side_effect = [0, 0]

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/executive/my-day")

    assert resp.status_code == 200
    tasks = resp.json()["tasks"]
    assert len(tasks) == 2
    assert tasks[0]["status"] == "todo"
    assert tasks[1]["status"] == "done"
    assert tasks[1]["completed_at"] is not None


# ---------------------------------------------------------------------------
# Tests: POST /api/executive/tasks (quick-add)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quick_add_task_standalone(exec_app):
    """POST /api/executive/tasks with just title returns 201."""
    import datetime

    app, conn = exec_app
    now = datetime.datetime.now(tz=datetime.UTC).isoformat()
    conn.fetchrow.return_value = FakeRecord(
        id=99,
        project_id=None,
        title="Buy groceries",
        priority=3,
        status="todo",
        deadline=None,
        description=None,
        parent_task_id=None,
        estimated_hours=None,
        actual_hours=None,
        sort_order=0,
        completed_at=None,
        created_at=now,
        updated_at=now,
    )

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/executive/tasks", json={"title": "Buy groceries"})

    assert resp.status_code == 201
    assert resp.json()["title"] == "Buy groceries"
    assert resp.json()["project_id"] is None


@pytest.mark.asyncio
async def test_quick_add_task_with_project(exec_app):
    """POST /api/executive/tasks with valid project_id returns 201."""
    import datetime

    app, conn = exec_app
    now = datetime.datetime.now(tz=datetime.UTC).isoformat()
    conn.fetchval.return_value = 10  # project exists
    conn.fetchrow.return_value = FakeRecord(
        id=100,
        project_id=10,
        title="Fix bug",
        priority=2,
        status="todo",
        deadline=None,
        description=None,
        parent_task_id=None,
        estimated_hours=None,
        actual_hours=None,
        sort_order=0,
        completed_at=None,
        created_at=now,
        updated_at=now,
    )

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/executive/tasks",
            json={"title": "Fix bug", "project_id": 10, "priority": 2},
        )

    assert resp.status_code == 201
    assert resp.json()["project_id"] == 10


@pytest.mark.asyncio
async def test_quick_add_task_invalid_project(exec_app):
    """POST /api/executive/tasks with bad project_id returns 404."""
    app, conn = exec_app
    conn.fetchval.return_value = None  # project doesn't exist

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/executive/tasks",
            json={"title": "Some task", "project_id": 99999},
        )

    assert resp.status_code == 404
    assert "project" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_focus_log_negative_hours_returns_422(exec_app):
    app, _ = exec_app
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/executive/focus/log", json={"duration_hours": -1})
        assert resp.status_code == 422


@pytest.mark.asyncio
async def test_focus_log_excessive_hours_returns_422(exec_app):
    app, _ = exec_app
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/executive/focus/log", json={"duration_hours": 25})
        assert resp.status_code == 422


@pytest.mark.asyncio
async def test_quick_add_task_empty_title_returns_422(exec_app):
    app, _ = exec_app
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/executive/tasks", json={"title": ""})
        assert resp.status_code == 422


@pytest.mark.asyncio
async def test_quick_add_task_invalid_priority_returns_422(exec_app):
    app, _ = exec_app
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/api/executive/tasks", json={"title": "Valid", "priority": 0})
        assert resp.status_code == 422
        resp2 = await client.post("/api/executive/tasks", json={"title": "Valid", "priority": 5})
        assert resp2.status_code == 422


@pytest.mark.asyncio
async def test_focus_log_task_not_found_no_side_effects(exec_app):
    """When task_id doesn't exist, 404 fires inside transaction — no DML executed."""
    app, conn = exec_app
    conn.fetchrow.return_value = None  # task doesn't exist (SELECT FOR UPDATE → None)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/executive/focus/log",
            json={"duration_hours": 0.5, "task_id": 99999},
        )
    assert resp.status_code == 404
    # Verify no DML was issued (execute should NOT be called for daily_log)
    conn.execute.assert_not_called()


# ---------------------------------------------------------------------------
# LE-009 concurrency regression — SELECT FOR UPDATE prevents double rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_focus_log_concurrent_requests_no_duplicate_rows(exec_app):
    """LE-009: Two concurrent log_focus_session calls must not produce duplicate daily_log rows.

    The mock verifies that each request uses a transaction + fetchrow (SELECT FOR UPDATE)
    before its DML, ensuring the serialised upsert path is exercised.  In production,
    PostgreSQL row-locking prevents concurrent INSERTs; here we assert the correct call
    sequence (fetchrow → execute) occurs for both concurrent requests.
    """
    import asyncio

    app, conn = exec_app

    # Both requests target an existing task — fetchrow returns a row for each call.
    conn.fetchrow.return_value = FakeRecord(id=1)
    conn.execute.return_value = "UPDATE 1"

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        results = await asyncio.gather(
            client.post("/api/executive/focus/log", json={"duration_hours": 0.5, "task_id": 1}),
            client.post("/api/executive/focus/log", json={"duration_hours": 0.5, "task_id": 1}),
        )

    # Both requests must succeed.
    assert all(r.status_code == 200 for r in results)
    assert all(r.json()["status"] == "success" for r in results)

    # fetchrow (SELECT ... FOR UPDATE) must have been called twice — once per request.
    assert conn.fetchrow.call_count == 2

    # The FOR UPDATE SQL must appear in both fetchrow calls.
    for call in conn.fetchrow.call_args_list:
        sql = call[0][0]
        assert "FOR UPDATE" in sql.upper(), f"Expected FOR UPDATE in SQL: {sql!r}"
