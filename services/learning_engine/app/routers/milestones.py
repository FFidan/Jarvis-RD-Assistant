"""Milestones CRUD router."""

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request
from jarvis_common import delete_or_404, dynamic_update

from app.deps import get_db_pool, limiter
from app.models import MilestoneCreate, MilestoneResponse, MilestoneUpdate

router = APIRouter(tags=["milestones"])

_MILESTONE_ALLOWED_COLUMNS: set[str] = {"name", "description", "deadline", "completed"}


# ---------------------------------------------------------------------------
# GET /api/projects/{project_id}/milestones
# ---------------------------------------------------------------------------


@router.get("/api/projects/{project_id}/milestones", response_model=list[MilestoneResponse])
@limiter.limit("60/minute")
async def list_milestones(
    request: Request,
    project_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> list[MilestoneResponse]:
    """List milestones for a project."""
    async with db_pool.acquire() as conn:
        project = await conn.fetchval("SELECT id FROM projects WHERE id = $1", project_id)
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")

        rows = await conn.fetch(
            "SELECT * FROM milestones WHERE project_id = $1"
            " ORDER BY deadline ASC NULLS LAST, created_at",
            project_id,
        )
    return [MilestoneResponse(**dict(row)) for row in rows]


# ---------------------------------------------------------------------------
# POST /api/projects/{project_id}/milestones
# ---------------------------------------------------------------------------


@router.post(
    "/api/projects/{project_id}/milestones",
    response_model=MilestoneResponse,
    status_code=201,
)
@limiter.limit("30/minute")
async def create_milestone(
    request: Request,
    project_id: int,
    body: MilestoneCreate,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> MilestoneResponse:
    """Create a milestone in a project."""
    async with db_pool.acquire() as conn:
        project = await conn.fetchval("SELECT id FROM projects WHERE id = $1", project_id)
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")

        row = await conn.fetchrow(
            """
            INSERT INTO milestones (project_id, name, deadline, description)
            VALUES ($1, $2, $3, $4)
            RETURNING *
            """,
            project_id,
            body.name,
            body.deadline,
            body.description,
        )
    return MilestoneResponse(**dict(row))


# ---------------------------------------------------------------------------
# PUT /api/milestones/{milestone_id}
# ---------------------------------------------------------------------------


@router.put("/api/milestones/{milestone_id}", response_model=MilestoneResponse)
@limiter.limit("30/minute")
async def update_milestone(
    request: Request,
    milestone_id: int,
    body: MilestoneUpdate,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> MilestoneResponse:
    """Update a milestone. Auto-sets completed_at when completed becomes True."""
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            existing = await conn.fetchrow(
                "SELECT * FROM milestones WHERE id = $1 FOR UPDATE", milestone_id
            )
            if not existing:
                raise HTTPException(status_code=404, detail="Milestone not found")

            updates_dict = body.model_dump(exclude_unset=True, include=_MILESTONE_ALLOWED_COLUMNS)
            if not updates_dict:
                return MilestoneResponse(**dict(existing))

            # Auto-set completed_at when completed transitions to/from True
            extra_sets: list[str] = []
            if updates_dict.get("completed") is True and not existing["completed"]:
                extra_sets.append("completed_at = NOW()")
            elif updates_dict.get("completed") is False:
                extra_sets.append("completed_at = NULL")

            row = await dynamic_update(
                conn,
                "milestones",
                milestone_id,
                updates_dict,
                _MILESTONE_ALLOWED_COLUMNS,
                extra_sets=extra_sets or None,
            )
            if row is None:
                raise HTTPException(status_code=404, detail="Record deleted during update")
    return MilestoneResponse(**dict(row))


# ---------------------------------------------------------------------------
# DELETE /api/milestones/{milestone_id}
# ---------------------------------------------------------------------------


@router.delete("/api/milestones/{milestone_id}", status_code=204)
@limiter.limit("30/minute")
async def delete_milestone(
    request: Request,
    milestone_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> None:
    """Delete a milestone."""
    await delete_or_404(
        db_pool,
        "DELETE FROM milestones WHERE id = $1",
        milestone_id,
        detail="Milestone not found",
    )
