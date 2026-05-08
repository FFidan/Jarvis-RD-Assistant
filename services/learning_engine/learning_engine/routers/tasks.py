"""Tasks CRUD router with paper-link management."""

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from jarvis_common import delete_or_404, dynamic_update, log_audit
from jarvis_common.auth import current_user_id_or_none

from learning_engine.deps import get_db_pool, limiter
from learning_engine.models import (
    TaskCreate,
    TaskPaperLinkCreate,
    TaskPaperLinkResponse,
    TaskResponse,
    TaskUpdate,
)

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
) -> list[TaskResponse]:
    """List tasks for a project, optionally filtered by status."""
    if status is not None and status not in _VALID_TASK_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid status: {status}. Valid values: {sorted(_VALID_TASK_STATUSES)}",
        )
    user_id = await current_user_id_or_none(request)
    async with db_pool.acquire() as conn:
        # Verify project exists and belongs to the caller (same conn as data query — avoid TOCTOU)
        project = await conn.fetchval(
            "SELECT id FROM projects WHERE id = $1 AND user_id IS NOT DISTINCT FROM $2",
            project_id,
            user_id,
        )
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")

        if status:
            rows = await conn.fetch(
                """SELECT * FROM tasks
                   WHERE project_id = $1 AND status = $2
                     AND user_id IS NOT DISTINCT FROM $3
                   ORDER BY sort_order, created_at""",
                project_id,
                status,
                user_id,
            )
        else:
            rows = await conn.fetch(
                """SELECT * FROM tasks
                   WHERE project_id = $1
                     AND user_id IS NOT DISTINCT FROM $2
                   ORDER BY sort_order, created_at""",
                project_id,
                user_id,
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
) -> TaskResponse:
    """Create a task in a project."""
    user_id = await current_user_id_or_none(request)
    async with db_pool.acquire() as conn:
        # Verify project exists and belongs to the caller (same conn as insert — avoid TOCTOU)
        project = await conn.fetchval(
            "SELECT id FROM projects WHERE id = $1 AND user_id IS NOT DISTINCT FROM $2",
            project_id,
            user_id,
        )
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")

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
) -> TaskResponse:
    """Update a task. Auto-sets completed_at when status changes to done."""
    user_id = await current_user_id_or_none(request)
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            existing = await conn.fetchrow(
                "SELECT * FROM tasks WHERE id = $1 AND user_id IS NOT DISTINCT FROM $2 FOR UPDATE",
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
            if updates_dict.get("status") == "done" and existing["status"] != "done":
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
) -> None:
    """Delete a task."""
    user_id = await current_user_id_or_none(request)
    await delete_or_404(
        db_pool,
        "DELETE FROM tasks WHERE id = $1 AND user_id IS NOT DISTINCT FROM $2",
        task_id,
        user_id,
        detail="Task not found",
    )
    await log_audit(
        db_pool,
        action="delete",
        resource=f"task:{task_id}",
        user_id=str(user_id) if user_id is not None else None,
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
) -> dict:
    """Link a paper to a task."""
    user_id = await current_user_id_or_none(request)
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            task = await conn.fetchval(
                "SELECT id FROM tasks WHERE id = $1 AND user_id IS NOT DISTINCT FROM $2 FOR UPDATE",
                task_id,
                user_id,
            )
            if not task:
                raise HTTPException(status_code=404, detail="Task not found")
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
) -> None:
    """Remove a paper link from a task."""
    user_id = await current_user_id_or_none(request)
    # Verify task ownership before deleting the link
    async with db_pool.acquire() as conn:
        task = await conn.fetchval(
            "SELECT id FROM tasks WHERE id = $1 AND user_id IS NOT DISTINCT FROM $2",
            task_id,
            user_id,
        )
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
    await delete_or_404(
        db_pool,
        "DELETE FROM task_paper_links WHERE task_id = $1 AND paper_id = $2",
        task_id,
        paper_id,
        detail="Link not found",
    )
    await log_audit(
        db_pool,
        action="delete",
        resource=f"task:{task_id}",
        user_id=str(user_id) if user_id is not None else None,
    )
