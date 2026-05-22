"""Tests for project_questions CRUD, activity UNION feed, and the widened
project list/detail counts (Projects IA redesign §3.4/§3.5/§3.6/§4).

All endpoints are exercised via ``__wrapped__`` to bypass the slowapi rate
limiter, mirroring the established pattern in test_wave3_le_scoping.py.
"""

from __future__ import annotations

import types
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from learning_engine.models import ProjectQuestionCreate
from learning_engine.routers import project_questions

from tests.conftest import make_pool_and_conn

_NOW = datetime(2026, 5, 16, 12, 0, 0, tzinfo=UTC)


def _request(user_id: int = 7):
    return types.SimpleNamespace(state=types.SimpleNamespace(user_id=user_id))


def _pool(conn):
    """Wrap an existing conn in a pool mock with transaction support."""
    pool, _ = make_pool_and_conn(conn=conn)
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
    # owner guard fired with the caller's user_id (predicate exercised by contract suite)
    _, *owner_args = conn.fetchval.call_args.args
    assert owner_args == [1, 7]


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
    _, *args = conn.fetchrow.call_args.args
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
    _, *args = conn.execute.call_args.args
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


# test_activity_union_ordering_and_kind_labels deleted — D6 SQL-text B1-09;
# survivor: test_project_questions_contract.py (A211) tests ordering + kinds
# against real PostgreSQL.


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


# test_activity_union_task_arm_scoped_by_user_id deleted — SQL-text B1-09;
# survivor: test_project_questions_contract.py (A211) verifies user_id scoping
# against real PostgreSQL.

# test_activity_union_milestone_arm_scoped_by_user_id deleted — SQL-text B1-09;
# same survivor as above.


@pytest.mark.asyncio
async def test_activity_union_user_id_bound_correctly() -> None:
    """LE-OB4: $2 is always the authenticated user_id, $3 is always the limit.

    Cross-checks the full parameter binding so a future refactor cannot
    accidentally swap $2/$3 or omit user_id from the bind list.
    """
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=1)
    conn.fetch = AsyncMock(return_value=[])
    await project_questions.list_project_activity.__wrapped__(
        _request(42), project_id=5, limit=10, db_pool=_pool(conn), user_id=42
    )
    _, *args = conn.fetch.call_args.args
    # positional args: project_id, user_id, limit
    assert args[0] == 5, f"$1 must be project_id=5; got {args[0]!r}"
    assert args[1] == 42, f"$2 must be user_id=42; got {args[1]!r}"
    assert args[2] == 10, f"$3 must be limit=10; got {args[2]!r}"


# ---------------------------------------------------------------------------
# Widened list/detail counts (§3.6 / §4c)
# ---------------------------------------------------------------------------


# test_list_projects_counts_present_in_both_branches deleted — SQL-text B1-09
# ("paper_count" in sql, "project_questions" in sql);
# survivor: test_projects_contract.py verifies counts fields are present in
# real DB responses.

# test_get_project_detail_includes_counts deleted — SQL-text B1-09
# ("project_papers" in counts_sql, "project_questions" in counts_sql);
# same survivor.
