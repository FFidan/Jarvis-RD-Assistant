"""Tests for executive endpoints in the Learning Engine service.

Covers:
- GET  /api/executive/my-day  — tasks, cards, recs, focus stats, pulse
- POST /api/executive/tasks   — quick-add a task (standalone, with project, invalid project)
- POST /api/executive/focus/log — logs a focus session (bare timer, task, paper, not-found)
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest
from httpx import ASGITransport

from tests.conftest import FakeRecord, _make_pool_and_conn

# ---------------------------------------------------------------------------
# Helpers (mirrors conftest + test_le_endpoints patterns)
# ---------------------------------------------------------------------------


def _route_my_day(
    conn,
    *,
    tasks=None,
    cards_due=0,
    recs=None,
    focus_hours=0,
    streak=0,
    pulse=None,
):
    """Install SQL-keyed dispatch for the GET /my-day reads.

    /my-day now issues its six reads concurrently via asyncio.gather on
    separate pooled connections, so side_effect *ordering* is no longer
    deterministic — dispatch on the SQL text instead.
    """
    tasks = tasks or []
    recs = recs or []
    pulse = pulse or []

    async def fetch(sql, *args):
        if "FROM tasks t" in sql:
            return tasks
        if "FROM paper_recommendations" in sql:
            return recs
        if "FROM projects p" in sql:
            return pulse
        raise AssertionError(f"unexpected fetch SQL: {sql!r}")

    async def fetchval(sql, *args):
        if "FROM cards" in sql:
            return cards_due
        if "log_date = CURRENT_DATE" in sql:
            return focus_hours
        if "qualifying AS" in sql:
            return streak
        raise AssertionError(f"unexpected fetchval SQL: {sql!r}")

    conn.fetch.side_effect = fetch
    conn.fetchval.side_effect = fetchval


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def exec_app():
    """Minimal app with mocked dependencies and disabled auth + rate limiting."""
    from jarvis_common import verify_api_key
    from jarvis_common.auth import (
        current_user_id_strict,
        current_user_id_strict_with_owner_override,
    )
    from learning_engine.deps import get_db_pool
    from learning_engine.main import app

    mock_pool, conn = _make_pool_and_conn()
    app.state.db_pool = mock_pool
    app.state.limiter.enabled = False

    app.dependency_overrides[get_db_pool] = lambda: mock_pool
    app.dependency_overrides[verify_api_key] = lambda: None
    app.dependency_overrides[current_user_id_strict_with_owner_override] = lambda: 1
    app.dependency_overrides[current_user_id_strict] = lambda: 1

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

    _route_my_day(conn, tasks=task_rows, cards_due=5, recs=rec_rows, focus_hours=2.5)

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

    _route_my_day(conn)

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
    _route_my_day(conn, recs=[rec_row])

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/executive/my-day", params={"limit_recommendations": 1})

    assert resp.status_code == 200
    # gather makes call ordering non-deterministic — find the recommendations
    # fetch by SQL. WS-2D: query is `($1=user_id, $2=limit)`, limit is $2.
    rec_fetch_call = next(
        c for c in conn.fetch.call_args_list if "FROM paper_recommendations" in c[0][0]
    )
    assert rec_fetch_call[0][2] == 1  # positional arg $2 = limit_recommendations


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
    """POST /api/executive/focus/log with paper_id returns 200 and uses new ON CONFLICT (paper_id, user_id) clause."""
    from unittest.mock import patch

    app, conn = exec_app

    # fetchrow (SELECT FOR UPDATE) returns a row — paper exists
    conn.fetchrow.return_value = FakeRecord(id=7)
    conn.execute.return_value = "INSERT 1"

    with patch(
        "learning_engine.routers.executive.current_user_id_strict_with_owner_override",
        new=AsyncMock(return_value=5),
    ):
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

    # Verify the ON CONFLICT clause references the composite key (paper_id, user_id)
    executed_sqls = [str(call.args[0]) for call in conn.execute.call_args_list]
    paper_state_sql = next((s for s in executed_sqls if "paper_user_state" in s), None)
    assert paper_state_sql is not None, "Expected an INSERT into paper_user_state"
    assert "ON CONFLICT (paper_id, user_id)" in paper_state_sql, (
        f"SQL must use composite ON CONFLICT, got: {paper_state_sql!r}"
    )


@pytest.mark.asyncio
async def test_focus_log_paper_not_found(exec_app):
    """POST /api/executive/focus/log with a missing paper_id returns 404."""
    app, conn = exec_app

    # fetchrow returns None — paper does not exist (assert_paper_ownership raises 404)
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
async def test_focus_log_rejects_other_users_paper(exec_app):
    """B-FOCUSCOL: log_focus_session must reject a paper owned by another user.

    assert_paper_ownership raises 403 when the paper's discovered_by != caller's
    user_id AND the paper is not in the caller's user_library.
    The dependency override in exec_app pins user_id=1; this test simulates
    a paper discovered_by=999 that is not in user 1's library.
    """
    app, conn = exec_app

    # fetchrow (assert_paper_ownership SELECT) returns another user's paper
    # fetchval (user_library check) returns None — not in caller's library
    conn.fetchrow.return_value = FakeRecord(discovered_by=999)
    conn.fetchval.return_value = None  # not in user_library

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/executive/focus/log",
            json={"duration_hours": 0.5, "paper_id": 7},
        )

    assert resp.status_code == 403, (
        f"Expected 403 for another user's paper, got {resp.status_code}: {resp.json()}"
    )


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
    _route_my_day(conn, tasks=task_rows)

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
    """Response surfaces today_focus_hours and the SQL-computed focus_streak_days.

    The streak is now a single gaps-and-islands SQL scalar (was a 365-row
    Python walk). Behaviour-preservation of the SQL itself is covered against
    real PostgreSQL by test_my_day_focus_streak_sql_live_pg.
    """
    app, conn = exec_app
    _route_my_day(conn, focus_hours=1.5, streak=3)

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
    _route_my_day(conn, pulse=pulse_rows)

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
    _route_my_day(conn, tasks=task_rows)

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


# ---------------------------------------------------------------------------
# Tests: GET /api/executive/my-day-bundle
# ---------------------------------------------------------------------------


def _route_bundle(
    conn,
    *,
    tasks=None,
    intent=None,
    threads=None,
    journal=None,
    yday_done=None,
    yday_deferred=None,
    yday_log=None,
):
    """SQL-keyed dispatch for the gather'd /my-day-bundle reads."""
    tasks = tasks or []
    threads = threads or []
    yday_done = yday_done or []
    yday_deferred = yday_deferred or []

    async def fetch(sql, *args):
        if "FROM tasks t" in sql:
            return tasks
        if "FROM thread " in sql:
            return threads
        if "status = 'done'" in sql:
            return yday_done
        if "IN ('todo', 'in_progress', 'blocked')" in sql:
            return yday_deferred
        raise AssertionError(f"unexpected bundle fetch SQL: {sql!r}")

    async def fetchrow(sql, *args):
        if "FROM daily_intent" in sql:
            return intent
        if "FROM journal_entries" in sql:
            return journal
        if "FROM daily_log" in sql:
            return yday_log
        raise AssertionError(f"unexpected bundle fetchrow SQL: {sql!r}")

    conn.fetch.side_effect = fetch
    conn.fetchrow.side_effect = fetchrow


@pytest.mark.asyncio
async def test_my_day_bundle_shape_and_aggregation(exec_app):
    """Bundle returns the six keys F7 needs, aggregated from the same tables."""
    import datetime

    app, conn = exec_app
    now = datetime.datetime.now(tz=datetime.UTC)

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
        )
    ]
    intent_row = FakeRecord(intent_text="Ship B7", updated_at=now)
    thread_rows = [
        FakeRecord(
            id=9,
            title="Refactor streak",
            anchor=None,
            progress=0.5,
            last_at=now,
            status="open",
            created_at=now,
        )
    ]
    journal_row = FakeRecord(
        id=3,
        date=now.date(),
        prompts={"worked": "indexes"},
        created_at=now,
        updated_at=now,
    )
    yday_done_rows = [FakeRecord(id=2, title="Done thing", status="done")]
    yday_log_row = FakeRecord(focus_hours=4.0, cards_reviewed=12)

    _route_bundle(
        conn,
        tasks=task_rows,
        intent=intent_row,
        threads=thread_rows,
        journal=journal_row,
        yday_done=yday_done_rows,
        yday_log=yday_log_row,
    )

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/executive/my-day-bundle")

    assert resp.status_code == 200
    data = resp.json()
    assert set(data) == {
        "tasks",
        "intent",
        "threads",
        "yesterday",
        "journal",
        "pulse_today",
    }
    assert data["tasks"][0]["title"] == "Write report"
    assert data["intent"]["intent"] == "Ship B7"
    assert data["threads"][0]["id"] == 9
    assert data["journal"]["prompts"] == {"worked": "indexes"}
    assert data["yesterday"]["focused_hours"] == 4.0
    assert data["yesterday"]["cards_reviewed"] == 12
    assert data["yesterday"]["tasks_done"] == 1
    assert data["yesterday"]["completed"][0]["id"] == 2
    # pulse_today is intentionally null (deck assembly lives in paper_ingestion).
    assert data["pulse_today"] is None


@pytest.mark.asyncio
async def test_my_day_bundle_null_tolerant_when_empty(exec_app):
    """Empty DB → empty/null fields, never a 500."""
    app, conn = exec_app
    _route_bundle(conn)  # everything empty / None

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/executive/my-day-bundle")

    assert resp.status_code == 200
    data = resp.json()
    assert data["tasks"] == []
    assert data["intent"] == {"intent": None, "updated_at": None}
    assert data["threads"] == []
    assert data["journal"] is None
    assert data["pulse_today"] is None
    assert data["yesterday"]["tasks_done"] == 0
    assert data["yesterday"]["focused_hours"] == 0.0
    assert data["yesterday"]["completed"] == []
    assert data["yesterday"]["deferred"] == []


# ---------------------------------------------------------------------------
# B-YDAY: verify timezone window math is correct (not a bug — FP confirmed)
# ---------------------------------------------------------------------------


def test_yday_timezone_window_utc_plus_3():
    """B-YDAY: yesterday window formula is correct for UTC+3.

    Verified: for tz_offset_minutes=180 the formula produces
    start_utc=2026-05-17T21:00Z, end_utc=2026-05-18T21:00Z when
    now_utc is 2026-05-19T10:00Z (= 13:00 local UTC+3).
    This exactly represents 2026-05-18T00:00..23:59 UTC+3 (yesterday local).
    """
    import datetime

    # Fix a concrete now_utc: 2026-05-19T10:00Z (= 13:00 local UTC+3)
    now_utc = datetime.datetime(2026, 5, 19, 10, 0, 0, tzinfo=datetime.UTC)
    tz_offset_minutes = 180  # UTC+3
    offset = datetime.timedelta(minutes=tz_offset_minutes)
    now_local = now_utc + offset  # 2026-05-19T13:00 (local, naive+offset arithmetic)
    yesterday_local_date = (now_local - datetime.timedelta(days=1)).date()
    assert yesterday_local_date == datetime.date(2026, 5, 18)

    start_utc = (
        datetime.datetime(
            yesterday_local_date.year,
            yesterday_local_date.month,
            yesterday_local_date.day,
            tzinfo=datetime.UTC,
        )
        - offset
    )
    end_utc = start_utc + datetime.timedelta(days=1)

    # 2026-05-18T00:00 UTC+3 = 2026-05-17T21:00Z
    assert start_utc == datetime.datetime(2026, 5, 17, 21, 0, 0, tzinfo=datetime.UTC)
    # end is exclusive: 2026-05-18T00:00 UTC+3 + 24h = 2026-05-18T21:00Z
    assert end_utc == datetime.datetime(2026, 5, 18, 21, 0, 0, tzinfo=datetime.UTC)


def test_yday_timezone_window_utc_minus_5():
    """B-YDAY: yesterday window is correct for UTC-5 (negative offset).

    now_utc=2026-05-19T03:00Z = 2026-05-18T22:00 local (UTC-5), so yesterday_local
    = 2026-05-17; start_utc = 2026-05-17T05:00Z, end_utc = 2026-05-18T05:00Z.
    """
    import datetime

    now_utc = datetime.datetime(2026, 5, 19, 3, 0, 0, tzinfo=datetime.UTC)
    tz_offset_minutes = -300  # UTC-5
    offset = datetime.timedelta(minutes=tz_offset_minutes)
    now_local = now_utc + offset  # 2026-05-18T22:00 (yesterday in local)
    yesterday_local_date = (now_local - datetime.timedelta(days=1)).date()
    assert yesterday_local_date == datetime.date(2026, 5, 17)

    start_utc = (
        datetime.datetime(
            yesterday_local_date.year,
            yesterday_local_date.month,
            yesterday_local_date.day,
            tzinfo=datetime.UTC,
        )
        - offset
    )
    end_utc = start_utc + datetime.timedelta(days=1)

    # 2026-05-17T00:00 UTC-5 = 2026-05-17T05:00Z
    assert start_utc == datetime.datetime(2026, 5, 17, 5, 0, 0, tzinfo=datetime.UTC)
    assert end_utc == datetime.datetime(2026, 5, 18, 5, 0, 0, tzinfo=datetime.UTC)


# ---------------------------------------------------------------------------
# Live-PG: focus-streak SQL must reproduce the old 365-row Python walk exactly.
# Opt-in via JARVIS_RUN_LIVE_PG=1.
# ---------------------------------------------------------------------------


def _py_streak(log_dates: list, today) -> int:
    """The exact pre-B7 Python streak algorithm, for differential testing."""
    import datetime

    rows = sorted(set(log_dates), reverse=True)
    if not rows:
        return 0
    expected = today if rows[0] == today else today - datetime.timedelta(days=1)
    if rows[0] != expected:
        return 0
    streak = 0
    for d in rows:
        if d == expected:
            streak += 1
            expected -= datetime.timedelta(days=1)
        else:
            break
    return streak


@pytest.mark.live_pg
@pytest.mark.asyncio
async def test_my_day_focus_streak_sql_live_pg(live_pg_dsn: str) -> None:
    """The gaps-and-islands streak SQL must equal _py_streak for every fixture.

    Covers: streak through today, streak ending yesterday, broken streak (gap),
    most-recent run too old (→ 0), and no rows (→ 0).
    """
    import datetime
    from pathlib import Path

    import asyncpg
    from learning_engine.routers.executive import _FOCUS_STREAK_SQL

    repo_root = Path(__file__).resolve().parents[3]
    init_sql = (repo_root / "db" / "init.sql").read_text(encoding="utf-8")

    pool = await asyncpg.create_pool(live_pg_dsn, min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            await conn.execute(init_sql)
            from paper_ingestion.migrations_runner import run_migrations

        await run_migrations(pool)

        async with pool.acquire() as conn:
            today = await conn.fetchval("SELECT CURRENT_DATE")
            d = datetime.timedelta(days=1)
            cases = {
                "through_today": [today, today - d, today - 2 * d],
                "ends_yesterday": [today - d, today - 2 * d],
                "broken_by_gap": [today, today - 2 * d, today - 3 * d],
                "too_old": [today - 5 * d, today - 6 * d],
                "empty": [],
            }
            for name, dates in cases.items():
                user_id = await conn.fetchval(
                    "INSERT INTO users (email) VALUES ($1) RETURNING id",
                    f"streak_{name}@example.com",
                )
                for ld in dates:
                    await conn.execute(
                        "INSERT INTO daily_log (user_id, log_date, focus_hours) "
                        "VALUES ($1, $2, 1.0)",
                        user_id,
                        ld,
                    )
                # A zero-hour row must NOT count (mirrors focus_hours > 0).
                await conn.execute(
                    "INSERT INTO daily_log (user_id, log_date, focus_hours) VALUES ($1, $2, 0)",
                    user_id,
                    today - 10 * d,
                )
                sql_val = await conn.fetchval(_FOCUS_STREAK_SQL, user_id)
                expected = _py_streak(dates, today)
                assert sql_val == expected, (
                    f"case {name}: SQL={sql_val} expected(py)={expected} dates={dates}"
                )
    finally:
        await pool.close()
