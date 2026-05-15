"""Tests for project_questions CRUD, activity UNION feed, and the widened
project list/detail counts (Projects IA redesign §3.4/§3.5/§3.6/§4).

All endpoints are exercised via ``__wrapped__`` to bypass the slowapi rate
limiter, mirroring the established pattern in test_wave3_le_scoping.py.
"""

from __future__ import annotations

import types
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from learning_engine.models import ProjectQuestionCreate
from learning_engine.routers import project_questions
from learning_engine.routers.projects import get_project, list_projects

_NOW = datetime(2026, 5, 16, 12, 0, 0, tzinfo=UTC)


def _request(user_id: int = 7):
    return types.SimpleNamespace(state=types.SimpleNamespace(user_id=user_id))


def _pool(conn):
    txn = MagicMock()
    txn.__aenter__ = AsyncMock(return_value=None)
    txn.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx
    return pool


# ---------------------------------------------------------------------------
# Question CRUD + ownership scoping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_questions_owner_scoped_returns_rows() -> None:
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=1)  # project owned by caller
    conn.fetch = AsyncMock(
        return_value=[
            {"id": 1, "project_id": 1, "body": "Q one", "created_at": _NOW},
            {"id": 2, "project_id": 1, "body": "Q two", "created_at": _NOW},
        ]
    )
    out = await project_questions.list_project_questions.__wrapped__(
        _request(7), project_id=1, db_pool=_pool(conn), user_id=7
    )
    assert [r["body"] for r in out] == ["Q one", "Q two"]
    # owner guard fired with the caller's user_id
    owner_sql, *owner_args = conn.fetchval.call_args.args
    assert "WHERE id = $1 AND user_id = $2" in owner_sql
    assert owner_args == [1, 7]
    # list query also filters by user_id
    list_sql = conn.fetch.call_args.args[0]
    assert "project_id = $1 AND user_id = $2" in list_sql


@pytest.mark.asyncio
async def test_list_questions_404_for_other_users_project() -> None:
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=None)  # not owned by caller
    with pytest.raises(HTTPException) as exc:
        await project_questions.list_project_questions.__wrapped__(
            _request(99), project_id=1, db_pool=_pool(conn), user_id=99
        )
    assert exc.value.status_code == 404
    conn.fetch.assert_not_called()


@pytest.mark.asyncio
async def test_create_question_owner_scoped() -> None:
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=1)
    conn.fetchrow = AsyncMock(
        return_value={"id": 5, "project_id": 1, "body": "New Q", "created_at": _NOW}
    )
    out = await project_questions.create_project_question.__wrapped__(
        _request(7),
        project_id=1,
        body=ProjectQuestionCreate(body="New Q"),
        db_pool=_pool(conn),
        user_id=7,
    )
    assert out["id"] == 5
    insert_sql, *args = conn.fetchrow.call_args.args
    assert "INSERT INTO project_questions (project_id, user_id, body)" in insert_sql
    assert args == [1, 7, "New Q"]


@pytest.mark.asyncio
async def test_create_question_404_for_other_users_project() -> None:
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=None)
    with pytest.raises(HTTPException) as exc:
        await project_questions.create_project_question.__wrapped__(
            _request(99),
            project_id=1,
            body=ProjectQuestionCreate(body="X"),
            db_pool=_pool(conn),
            user_id=99,
        )
    assert exc.value.status_code == 404
    conn.fetchrow.assert_not_called()


@pytest.mark.asyncio
async def test_delete_question_scoped_by_user_id(monkeypatch) -> None:
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value="DELETE 1")
    monkeypatch.setattr(project_questions, "log_audit", AsyncMock(), raising=True)
    await project_questions.delete_project_question.__wrapped__(
        _request(7), question_id=5, db_pool=_pool(conn), user_id=7
    )
    del_sql, *args = conn.execute.call_args.args
    assert "DELETE FROM project_questions WHERE id = $1 AND user_id = $2" in del_sql
    assert args == [5, 7]


@pytest.mark.asyncio
async def test_delete_question_404_when_not_owned() -> None:
    """Another user's question is invisible — DELETE affects 0 rows → 404."""
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value="DELETE 0")
    with pytest.raises(HTTPException) as exc:
        await project_questions.delete_project_question.__wrapped__(
            _request(99), question_id=5, db_pool=_pool(conn), user_id=99
        )
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# Recent-activity UNION feed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_activity_union_ordering_and_kind_labels() -> None:
    t1 = datetime(2026, 5, 16, 9, 0, tzinfo=UTC)
    t2 = datetime(2026, 5, 16, 10, 0, tzinfo=UTC)
    t3 = datetime(2026, 5, 16, 11, 0, tzinfo=UTC)
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=1)
    # DB returns newest-first (ORDER BY ts DESC enforced in SQL).
    conn.fetch = AsyncMock(
        return_value=[
            {"kind": "completed_milestone", "ts": t3, "label": "M1"},
            {"kind": "completed_task", "ts": t2, "label": "Task A"},
            {"kind": "added_paper", "ts": t1, "label": "Paper X"},
        ]
    )
    out = await project_questions.list_project_activity.__wrapped__(
        _request(7), project_id=1, limit=20, db_pool=_pool(conn), user_id=7
    )
    assert [r["kind"] for r in out] == [
        "completed_milestone",
        "completed_task",
        "added_paper",
    ]
    assert [r["ts"] for r in out] == [t3, t2, t1]  # newest-first
    sql = conn.fetch.call_args.args[0]
    assert "UNION ALL" in sql
    assert sql.count("UNION ALL") == 2
    assert "ORDER BY ts DESC" in sql
    assert "t.status = 'done'" in sql
    assert "m.completed = TRUE" in sql


@pytest.mark.asyncio
async def test_activity_404_for_other_users_project() -> None:
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=None)
    with pytest.raises(HTTPException) as exc:
        await project_questions.list_project_activity.__wrapped__(
            _request(99), project_id=1, limit=20, db_pool=_pool(conn), user_id=99
        )
    assert exc.value.status_code == 404
    conn.fetch.assert_not_called()


# ---------------------------------------------------------------------------
# Widened list/detail counts (§3.6 / §4c)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_projects_counts_present_in_both_branches() -> None:
    base = {
        "id": 1,
        "name": "P",
        "description": None,
        "status": "active",
        "deadline": None,
        "color": None,
        "created_at": _NOW,
        "updated_at": _NOW,
    }

    # Unfiltered branch — zero counts.
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[{**base, "paper_count": 0, "open_question_count": 0}])
    out = await list_projects.__wrapped__(_request(7), status=None, db_pool=_pool(conn), user_id=7)
    assert out[0].paper_count == 0
    assert out[0].open_question_count == 0
    unfiltered_sql = conn.fetch.call_args.args[0]
    assert "paper_count" in unfiltered_sql
    assert "open_question_count" in unfiltered_sql
    assert "project_questions" in unfiltered_sql

    # Status-filtered branch — non-zero counts.
    conn2 = AsyncMock()
    conn2.fetch = AsyncMock(return_value=[{**base, "paper_count": 3, "open_question_count": 2}])
    out2 = await list_projects.__wrapped__(
        _request(7), status="active", db_pool=_pool(conn2), user_id=7
    )
    assert out2[0].paper_count == 3
    assert out2[0].open_question_count == 2
    filtered_sql = conn2.fetch.call_args.args[0]
    assert "paper_count" in filtered_sql
    assert "open_question_count" in filtered_sql


@pytest.mark.asyncio
async def test_get_project_detail_includes_counts() -> None:
    project_row = {
        "id": 1,
        "name": "P",
        "description": None,
        "status": "active",
        "deadline": None,
        "color": None,
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    counts_row = {
        "total_tasks": 4,
        "done_tasks": 1,
        "total_milestones": 2,
        "completed_milestones": 0,
        "paper_count": 5,
        "open_question_count": 3,
    }
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[project_row, counts_row])
    resp = await get_project.__wrapped__(_request(7), project_id=1, db_pool=_pool(conn), user_id=7)
    assert resp.paper_count == 5
    assert resp.open_question_count == 3
    counts_sql = conn.fetchrow.call_args_list[1].args[0]
    assert "project_papers" in counts_sql
    assert "project_questions" in counts_sql
