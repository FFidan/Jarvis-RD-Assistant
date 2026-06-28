"""Project open-questions CRUD + recent-activity feed.

Every endpoint is strictly project-owner-scoped: the project row is fetched
with ``WHERE id = $1 AND user_id = $2`` (IDOR guard reused verbatim from
``project_papers.list_project_papers``) and a 404 is raised when absent, so a
caller can never read/write another user's questions or activity.
"""

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from jarvis_common import log_audit
from jarvis_common.auth import current_user_id_strict

from learning_engine.deps import get_db_pool, limiter
from learning_engine.models import (
    ProjectActivityItem,
    ProjectQuestionCreate,
    ProjectQuestionResponse,
)
from learning_engine.routers._guards import assert_project_owner as _assert_project_owner

router = APIRouter(prefix="/api/projects", tags=["project-questions"])
# DELETE is addressed by question id, not nested under a project,
# so it lives on its own /api/questions prefix.
questions_router = APIRouter(prefix="/api/questions", tags=["project-questions"])


# ---------------------------------------------------------------------------
# GET /api/projects/{project_id}/questions
# ---------------------------------------------------------------------------


@router.get("/{project_id}/questions", response_model=list[ProjectQuestionResponse])
@limiter.limit("60/minute")
async def list_project_questions(
    request: Request,
    project_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(current_user_id_strict),
) -> list[ProjectQuestionResponse]:
    """List open research questions for a project (owner-scoped)."""
    async with db_pool.acquire() as conn:
        await _assert_project_owner(conn, project_id, user_id)
        rows = await conn.fetch(
            """
            SELECT id, project_id, body, created_at
            FROM project_questions
            WHERE project_id = $1 AND user_id = $2
            ORDER BY created_at ASC, id ASC
            """,
            project_id,
            user_id,
        )
    return [ProjectQuestionResponse(**dict(r)) for r in rows]


# ---------------------------------------------------------------------------
# POST /api/projects/{project_id}/questions
# ---------------------------------------------------------------------------


@router.post(
    "/{project_id}/questions",
    response_model=ProjectQuestionResponse,
    status_code=201,
)
@limiter.limit("30/minute")
async def create_project_question(
    request: Request,
    project_id: int,
    body: ProjectQuestionCreate,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(current_user_id_strict),
) -> ProjectQuestionResponse:
    """Add an open question to a project (owner-scoped)."""
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            await _assert_project_owner(conn, project_id, user_id)
            row = await conn.fetchrow(
                """
                INSERT INTO project_questions (project_id, user_id, body)
                VALUES ($1, $2, $3)
                RETURNING id, project_id, body, created_at
                """,
                project_id,
                user_id,
                body.body,
            )
    return ProjectQuestionResponse(**dict(row))


# ---------------------------------------------------------------------------
# DELETE /api/questions/{question_id}
# ---------------------------------------------------------------------------


@questions_router.delete("/{question_id}", status_code=204)
@limiter.limit("30/minute")
async def delete_project_question(
    request: Request,
    question_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(current_user_id_strict),
) -> None:
    """Delete an open question. Scoped by user_id — no cross-user delete."""
    async with db_pool.acquire() as conn:
        # IDOR guard: only the owner of the question row may delete it.
        result = await conn.execute(
            "DELETE FROM project_questions WHERE id = $1 AND user_id = $2",
            question_id,
            user_id,
        )
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail="Question not found")
    await log_audit(
        db_pool,
        action="delete",
        resource=f"project_question:{question_id}",
        user_id=str(user_id),
    )


# ---------------------------------------------------------------------------
# GET /api/projects/{project_id}/activity
# ---------------------------------------------------------------------------


@router.get("/{project_id}/activity", response_model=list[ProjectActivityItem])
@limiter.limit("60/minute")
async def list_project_activity(
    request: Request,
    project_id: int,
    limit: int = Query(default=20, ge=1, le=100),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(current_user_id_strict),
) -> list[ProjectActivityItem]:
    """Recent-activity feed: UNION over linked papers, completed
    tasks, and completed milestones — project-scoped via the owner guard,
    newest-first, each row carrying a ``kind`` label.
    """
    async with db_pool.acquire() as conn:
        await _assert_project_owner(conn, project_id, user_id)
        rows = await conn.fetch(
            """
            SELECT 'added_paper' AS kind, pp.added_at AS ts, p.title AS label
              FROM project_papers pp
              JOIN papers p ON p.id = pp.paper_id
             WHERE pp.project_id = $1
            UNION ALL
            SELECT 'completed_task' AS kind, t.completed_at AS ts, t.title AS label
              FROM tasks t
             WHERE t.project_id = $1
               AND t.user_id = $2
               AND t.status = 'done'
               AND t.completed_at IS NOT NULL
            UNION ALL
            SELECT 'completed_milestone' AS kind, m.completed_at AS ts, m.name AS label
              FROM milestones m
             WHERE m.project_id = $1
               AND m.user_id = $2
               AND m.completed = TRUE
               AND m.completed_at IS NOT NULL
            ORDER BY ts DESC
            LIMIT $3
            """,
            project_id,
            user_id,
            limit,
        )
    return [ProjectActivityItem(**dict(r)) for r in rows]
