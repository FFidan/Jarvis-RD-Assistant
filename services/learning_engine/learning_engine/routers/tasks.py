"""Tasks CRUD router with paper-link management."""

from typing import Any, Literal

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from jarvis_common import assert_paper_ownership, delete_or_404, dynamic_update, log_audit
from jarvis_common.auth import (
    current_user_id_strict,
    current_user_id_strict_with_owner_override,
)

from learning_engine.deps import get_db_pool, limiter
from learning_engine.models import (
    TaskCreate,
    TaskPaperLinkCreate,
    TaskPaperLinkResponse,
    TaskResponse,
    TaskUpdate,
)
from learning_engine.routers._guards import assert_project_owner as _assert_project_owner

router = APIRouter(prefix="/api", tags=["tasks"])

_TASK_ALLOWED_COLUMNS: set[str] = {
    "title",
    "status",
    "priority",
    "deadline",
    "description",
    "estimated_hours",
    "actual_hours",
    "sort_order",
}


# ---------------------------------------------------------------------------
# GET /api/projects/{project_id}/tasks
# ---------------------------------------------------------------------------

_VALID_TASK_STATUSES = frozenset({"todo", "in_progress", "done", "blocked"})


@router.get("/projects/{project_id}/tasks", response_model=list[TaskResponse])
@limiter.limit("60/minute")
async def list_tasks(
    request: Request,
    project_id: int,
    status: str | None = Query(default=None),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(current_user_id_strict_with_owner_override),
) -> list[TaskResponse]:
    """List tasks for a project, optionally filtered by status."""
    if status is not None and status not in _VALID_TASK_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid status: {status}. Valid values: {sorted(_VALID_TASK_STATUSES)}",
        )
    async with db_pool.acquire() as conn:
        # Verify project exists and belongs to the caller (same conn as data query — avoid TOCTOU)
        await _assert_project_owner(conn, project_id, user_id)

        if status:
            rows = await conn.fetch(
                """SELECT * FROM tasks
                   WHERE project_id = $1 AND status = $2
                     AND user_id = $3
                   ORDER BY sort_order, created_at""",
                project_id,
                status,
                user_id,
            )
        else:
            rows = await conn.fetch(
                """SELECT * FROM tasks
                   WHERE project_id = $1
                     AND user_id = $2
                   ORDER BY sort_order, created_at""",
                project_id,
                user_id,
            )
    return [TaskResponse(**dict(row)) for row in rows]


# ---------------------------------------------------------------------------
# GET /api/tasks  (cross-project task list)
# ---------------------------------------------------------------------------


@router.get("/tasks", response_model=list[TaskResponse])
@limiter.limit("60/minute")
async def list_all_tasks(
    request: Request,
    status: Literal["todo", "in_progress", "done", "blocked"] | None = None,
    project_id: int | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(current_user_id_strict_with_owner_override),
) -> list[TaskResponse]:
    """List the caller's tasks across all projects.

    LEFT JOIN on projects so quick-add tasks (``project_id IS NULL``) are
    included with ``project_name`` None. Scoped to the caller via
    ``t.user_id``; optional ``status`` / ``project_id`` narrow the result.
    """
    clauses = ["t.user_id = $1"]
    params: list[Any] = [user_id]
    if status is not None:
        params.append(status)
        clauses.append(f"t.status = ${len(params)}")
    if project_id is not None:
        params.append(project_id)
        clauses.append(f"t.project_id = ${len(params)}")
    params.append(limit)
    limit_pos = len(params)

    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            f"""SELECT t.*, pr.name AS project_name
                FROM tasks t
                LEFT JOIN projects pr ON pr.id = t.project_id
                WHERE {" AND ".join(clauses)}
                ORDER BY t.priority, t.created_at DESC
                LIMIT ${limit_pos}""",
            *params,
        )
    return [TaskResponse(**dict(row)) for row in rows]


# ---------------------------------------------------------------------------
# POST /api/projects/{project_id}/tasks
# ---------------------------------------------------------------------------


@router.post("/projects/{project_id}/tasks", response_model=TaskResponse, status_code=201)
@limiter.limit("30/minute")
async def create_task(
    request: Request,
    project_id: int,
    body: TaskCreate,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(current_user_id_strict),
) -> TaskResponse:
    """Create a task in a project."""
    async with db_pool.acquire() as conn:
        # Verify project exists and belongs to the caller (same conn as insert — avoid TOCTOU)
        await _assert_project_owner(conn, project_id, user_id)

        if body.parent_task_id is not None:
            parent = await conn.fetchval(
                "SELECT id FROM tasks WHERE id = $1 AND user_id = $2 AND project_id = $3",
                body.parent_task_id,
                user_id,
                project_id,
            )
            if parent is None:
                raise HTTPException(status_code=404, detail="Parent task not found")

        try:
            row = await conn.fetchrow(
                """
                INSERT INTO tasks (
                    project_id, parent_task_id, title, description,
                    status, priority, deadline, estimated_hours, user_id
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                RETURNING *
                """,
                project_id,
                body.parent_task_id,
                body.title,
                body.description,
                body.status,
                body.priority,
                body.deadline,
                body.estimated_hours,
                user_id,
            )
        except asyncpg.ForeignKeyViolationError as exc:
            constraint = getattr(exc, "constraint_name", "") or ""
            if "parent" in constraint:
                raise HTTPException(status_code=404, detail="Parent task not found") from exc
            raise HTTPException(status_code=404, detail="Project not found") from exc
    return TaskResponse(**dict(row))


# ---------------------------------------------------------------------------
# PUT /api/tasks/{task_id}
# ---------------------------------------------------------------------------


@router.put("/tasks/{task_id}", response_model=TaskResponse)
@limiter.limit("30/minute")
async def update_task(
    request: Request,
    task_id: int,
    body: TaskUpdate,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(current_user_id_strict_with_owner_override),
) -> TaskResponse:
    """Update a task. Auto-sets completed_at when status changes to done."""
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            existing = await conn.fetchrow(
                "SELECT * FROM tasks WHERE id = $1 AND user_id = $2 FOR UPDATE",
                task_id,
                user_id,
            )
            if not existing:
                raise HTTPException(status_code=404, detail="Task not found")

            updates_dict = body.model_dump(exclude_unset=True, include=_TASK_ALLOWED_COLUMNS)
            if not updates_dict:
                return TaskResponse(**dict(existing))

            # Auto-set completed_at when status transitions to/from done
            extra_sets = ["updated_at = NOW()"]
            transitioned_to_done = (
                updates_dict.get("status") == "done" and existing["status"] != "done"
            )
            if transitioned_to_done:
                extra_sets.append("completed_at = NOW()")
            elif updates_dict.get("status") and updates_dict["status"] != "done":
                extra_sets.append("completed_at = NULL")

            row = await dynamic_update(
                conn,
                "tasks",
                task_id,
                updates_dict,
                _TASK_ALLOWED_COLUMNS,
                extra_sets=extra_sets,
            )
            if row is None:
                raise HTTPException(status_code=404, detail="Task not found or was deleted")

            # BUG-1 (S8): update_task is the sole writer of daily_log.tasks_completed.
            # On a genuine todo→done transition, bump today's counter (COALESCE
            # because the column is nullable). The existing!=done guard makes a
            # re-PUT of an already-done task idempotent (no double-count).
            if transitioned_to_done:
                await conn.execute(
                    """INSERT INTO daily_log (user_id, log_date, tasks_completed)
                       VALUES ($1, CURRENT_DATE, 1)
                       ON CONFLICT (user_id, log_date)
                       DO UPDATE SET tasks_completed
                           = COALESCE(daily_log.tasks_completed, 0) + 1""",
                    user_id,
                )
    return TaskResponse(**dict(row))


# ---------------------------------------------------------------------------
# DELETE /api/tasks/{task_id}
# ---------------------------------------------------------------------------


@router.delete("/tasks/{task_id}", status_code=204)
@limiter.limit("30/minute")
async def delete_task(
    request: Request,
    task_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(current_user_id_strict),
) -> None:
    """Delete a task."""
    await delete_or_404(
        db_pool,
        "DELETE FROM tasks WHERE id = $1 AND user_id = $2",
        task_id,
        user_id,
        detail="Task not found",
    )
    await log_audit(
        db_pool,
        action="delete",
        resource=f"task:{task_id}",
        user_id=str(user_id),
    )


# ---------------------------------------------------------------------------
# POST /api/tasks/{task_id}/papers  (link paper to task)
# ---------------------------------------------------------------------------


@router.post("/tasks/{task_id}/papers", status_code=201, response_model=TaskPaperLinkResponse)
@limiter.limit("30/minute")
async def link_paper_to_task(
    request: Request,
    task_id: int,
    body: TaskPaperLinkCreate,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(current_user_id_strict),
) -> dict[str, Any]:
    """Link a paper to a task."""
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            task = await conn.fetchval(
                "SELECT id FROM tasks WHERE id = $1 AND user_id = $2 FOR UPDATE",
                task_id,
                user_id,
            )
            if not task:
                raise HTTPException(status_code=404, detail="Task not found")
            # Assert caller owns (or has library access to) the paper before linking.
            await assert_paper_ownership(conn, body.paper_id, user_id)
            try:
                row = await conn.fetchrow(
                    "INSERT INTO task_paper_links (task_id, paper_id, note)"
                    " VALUES ($1, $2, $3) RETURNING *",
                    task_id,
                    body.paper_id,
                    body.note,
                )
            except asyncpg.UniqueViolationError as e:
                raise HTTPException(
                    status_code=409, detail="Paper already linked to this task"
                ) from e
            except asyncpg.ForeignKeyViolationError as e:
                raise HTTPException(status_code=404, detail="Paper not found") from e
    return dict(row)


# ---------------------------------------------------------------------------
# DELETE /api/tasks/{task_id}/papers/{paper_id}  (unlink)
# ---------------------------------------------------------------------------


@router.delete("/tasks/{task_id}/papers/{paper_id}", status_code=204)
@limiter.limit("30/minute")
async def unlink_paper_from_task(
    request: Request,
    task_id: int,
    paper_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(current_user_id_strict),
) -> None:
    """Remove a paper link from a task."""
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            # Ownership check and DELETE in the same transaction for durability.
            task = await conn.fetchval(
                "SELECT id FROM tasks WHERE id = $1 AND user_id = $2",
                task_id,
                user_id,
            )
            if not task:
                raise HTTPException(status_code=404, detail="Task not found")
            result = await conn.execute(
                "DELETE FROM task_paper_links WHERE task_id = $1 AND paper_id = $2",
                task_id,
                paper_id,
            )
            if result == "DELETE 0":
                raise HTTPException(status_code=404, detail="Link not found")
    await log_audit(
        db_pool,
        action="delete",
        resource=f"task:{task_id}",
        user_id=str(user_id),
    )
