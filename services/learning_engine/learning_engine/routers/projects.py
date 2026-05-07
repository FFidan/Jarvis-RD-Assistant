"""Projects CRUD router."""

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from jarvis_common import delete_or_404, dynamic_update, log_audit
from jarvis_common.auth import current_user_id_or_none

from learning_engine.deps import get_db_pool, limiter
from learning_engine.models import (
    ProjectCreate,
    ProjectDetailResponse,
    ProjectResponse,
    ProjectUpdate,
)

router = APIRouter(prefix="/api/projects", tags=["projects"])

_PROJECT_ALLOWED_COLUMNS: set[str] = {"name", "description", "status", "deadline", "color"}
_VALID_STATUSES = frozenset({"active", "paused", "completed", "archived"})


# ---------------------------------------------------------------------------
# GET /api/projects
# ---------------------------------------------------------------------------


@router.get("", response_model=list[ProjectResponse])
@limiter.limit("60/minute")
async def list_projects(
    request: Request,
    status: str | None = Query(default=None),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> list[ProjectResponse]:
    """List all projects, optionally filtered by status."""
    if status and status not in _VALID_STATUSES:
        valid = ", ".join(sorted(_VALID_STATUSES))
        raise HTTPException(
            status_code=400,
            detail=f"Invalid status '{status}'. Must be one of: {valid}",
        )
    async with db_pool.acquire() as conn:
        if status:
            rows = await conn.fetch(
                "SELECT * FROM projects WHERE status = $1 ORDER BY created_at DESC",
                status,
            )
        else:
            rows = await conn.fetch("SELECT * FROM projects ORDER BY created_at DESC")
    return [ProjectResponse(**dict(row)) for row in rows]


# ---------------------------------------------------------------------------
# POST /api/projects
# ---------------------------------------------------------------------------


@router.post("", response_model=ProjectResponse, status_code=201)
@limiter.limit("30/minute")
async def create_project(
    request: Request,
    body: ProjectCreate,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> ProjectResponse:
    """Create a new project."""
    row = await db_pool.fetchrow(
        """
        INSERT INTO projects (name, description, status, deadline, color)
        VALUES ($1, $2, $3, $4, $5)
        RETURNING *
        """,
        body.name,
        body.description,
        body.status,
        body.deadline,
        body.color,
    )
    return ProjectResponse(**dict(row))


# ---------------------------------------------------------------------------
# GET /api/projects/{project_id}
# ---------------------------------------------------------------------------


@router.get("/{project_id}", response_model=ProjectDetailResponse)
@limiter.limit("60/minute")
async def get_project(
    request: Request,
    project_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
):
    """Get a project with task and milestone counts."""
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM projects WHERE id = $1", project_id)
        if not row:
            raise HTTPException(status_code=404, detail="Project not found")

        counts = await conn.fetchrow(
            """
            SELECT COALESCE(t.total, 0) AS total_tasks,
                   COALESCE(t.done, 0)  AS done_tasks,
                   COALESCE(m.total, 0) AS total_milestones,
                   COALESCE(m.done, 0)  AS completed_milestones
            FROM (SELECT 1) AS _
            LEFT JOIN LATERAL (
                SELECT COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE status = 'done') AS done
                FROM tasks WHERE project_id = $1
            ) t ON TRUE
            LEFT JOIN LATERAL (
                SELECT COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE completed = TRUE) AS done
                FROM milestones WHERE project_id = $1
            ) m ON TRUE
            """,
            project_id,
        )

    return ProjectDetailResponse(
        **dict(row),
        total_tasks=counts["total_tasks"],
        done_tasks=counts["done_tasks"],
        total_milestones=counts["total_milestones"],
        completed_milestones=counts["completed_milestones"],
    )


# ---------------------------------------------------------------------------
# PUT /api/projects/{project_id}
# ---------------------------------------------------------------------------


@router.put("/{project_id}", response_model=ProjectResponse)
@limiter.limit("30/minute")
async def update_project(
    request: Request,
    project_id: int,
    body: ProjectUpdate,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> ProjectResponse:
    """Update a project."""
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            existing = await conn.fetchrow(
                "SELECT * FROM projects WHERE id = $1 FOR UPDATE", project_id
            )
            if not existing:
                raise HTTPException(status_code=404, detail="Project not found")

            updates_dict = body.model_dump(exclude_unset=True, include=_PROJECT_ALLOWED_COLUMNS)
            if not updates_dict:
                return ProjectResponse(**dict(existing))

            row = await dynamic_update(
                conn,
                "projects",
                project_id,
                updates_dict,
                _PROJECT_ALLOWED_COLUMNS,
                extra_sets=["updated_at = NOW()"],
            )
            if row is None:
                raise HTTPException(status_code=404, detail="Record deleted during update")
    return ProjectResponse(**dict(row))


# ---------------------------------------------------------------------------
# DELETE /api/projects/{project_id}
# ---------------------------------------------------------------------------


@router.delete("/{project_id}", status_code=204)
@limiter.limit("30/minute")
async def delete_project(
    request: Request,
    project_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> None:
    """Delete a project (cascades to tasks and milestones)."""
    await delete_or_404(
        db_pool,
        "DELETE FROM projects WHERE id = $1",
        project_id,
        detail="Project not found",
    )
    user_id = await current_user_id_or_none(request)
    await log_audit(
        db_pool,
        action="delete",
        resource=f"project:{project_id}",
        user_id=str(user_id) if user_id is not None else None,
    )
