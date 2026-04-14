"""Tasks CRUD router with paper-link management."""

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from jarvis_common import delete_or_404, dynamic_update

from app.deps import get_db_pool, limiter
from app.models import (
    TaskCreate,
    TaskPaperLinkCreate,
    TaskPaperLinkResponse,
    TaskResponse,
    TaskUpdate,
)

router = APIRouter(tags=["tasks"])

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


@router.get("/api/projects/{project_id}/tasks", response_model=list[TaskResponse])
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
    async with db_pool.acquire() as conn:
        # Verify project exists (same connection as data query to avoid TOCTOU)
        project = await conn.fetchval("SELECT id FROM projects WHERE id = $1", project_id)
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")

        if status:
            rows = await conn.fetch(
                """SELECT * FROM tasks
                   WHERE project_id = $1 AND status = $2
                   ORDER BY sort_order, created_at""",
                project_id,
                status,
            )
        else:
            rows = await conn.fetch(
                """SELECT * FROM tasks
                   WHERE project_id = $1
                   ORDER BY sort_order, created_at""",
                project_id,
            )
    return [TaskResponse(**dict(row)) for row in rows]


# ---------------------------------------------------------------------------
# POST /api/projects/{project_id}/tasks
# ---------------------------------------------------------------------------


@router.post("/api/projects/{project_id}/tasks", response_model=TaskResponse, status_code=201)
@limiter.limit("30/minute")
async def create_task(
    request: Request,
    project_id: int,
    body: TaskCreate,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> TaskResponse:
    """Create a task in a project."""
    async with db_pool.acquire() as conn:
        # Verify project exists (same connection as insert to avoid TOCTOU)
        project = await conn.fetchval("SELECT id FROM projects WHERE id = $1", project_id)
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")

        try:
            row = await conn.fetchrow(
                """
                INSERT INTO tasks (
                    project_id, parent_task_id, title, description,
                    status, priority, deadline, estimated_hours
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
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
            )
        except asyncpg.ForeignKeyViolationError as exc:
            constraint = getattr(exc, "constraint_name", "") or ""
            if "parent" in constraint:
                raise HTTPException(status_code=404, detail="Parent task not found") from None
            raise HTTPException(status_code=404, detail="Project not found") from None
    return TaskResponse(**dict(row))


# ---------------------------------------------------------------------------
# PUT /api/tasks/{task_id}
# ---------------------------------------------------------------------------


@router.put("/api/tasks/{task_id}", response_model=TaskResponse)
@limiter.limit("30/minute")
async def update_task(
    request: Request,
    task_id: int,
    body: TaskUpdate,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> TaskResponse:
    """Update a task. Auto-sets completed_at when status changes to done."""
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            existing = await conn.fetchrow("SELECT * FROM tasks WHERE id = $1 FOR UPDATE", task_id)
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


@router.delete("/api/tasks/{task_id}", status_code=204)
@limiter.limit("30/minute")
async def delete_task(
    request: Request,
    task_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> None:
    """Delete a task."""
    await delete_or_404(
        db_pool,
        "DELETE FROM tasks WHERE id = $1",
        task_id,
        detail="Task not found",
    )


# ---------------------------------------------------------------------------
# POST /api/tasks/{task_id}/papers  (link paper to task)
# ---------------------------------------------------------------------------


@router.post("/api/tasks/{task_id}/papers", status_code=201, response_model=TaskPaperLinkResponse)
@limiter.limit("30/minute")
async def link_paper_to_task(
    request: Request,
    task_id: int,
    body: TaskPaperLinkCreate,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> dict:
    """Link a paper to a task."""
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            task = await conn.fetchval("SELECT id FROM tasks WHERE id = $1 FOR UPDATE", task_id)
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
            except asyncpg.UniqueViolationError:
                raise HTTPException(status_code=409, detail="Paper already linked to this task")
            except asyncpg.ForeignKeyViolationError:
                raise HTTPException(status_code=404, detail="Paper not found")
    return dict(row)


# ---------------------------------------------------------------------------
# DELETE /api/tasks/{task_id}/papers/{paper_id}  (unlink)
# ---------------------------------------------------------------------------


@router.delete("/api/tasks/{task_id}/papers/{paper_id}", status_code=204)
@limiter.limit("30/minute")
async def unlink_paper_from_task(
    request: Request,
    task_id: int,
    paper_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> None:
    """Remove a paper link from a task."""
    await delete_or_404(
        db_pool,
        "DELETE FROM task_paper_links WHERE task_id = $1 AND paper_id = $2",
        task_id,
        paper_id,
        detail="Link not found",
    )
