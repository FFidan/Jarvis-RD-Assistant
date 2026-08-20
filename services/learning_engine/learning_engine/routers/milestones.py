"""Milestones CRUD router."""

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from jarvis_common import delete_or_404, dynamic_update, log_audit
from jarvis_common.auth import current_user_id_strict

from learning_engine.deps import get_db_pool, limiter
from learning_engine.models import (
    MilestoneCreate,
    MilestoneDeadlineItem,
    MilestoneResponse,
    MilestoneUpdate,
)
from learning_engine.routers._guards import assert_project_owner as _assert_project_owner

router = APIRouter(prefix="/api", tags=["milestones"])

_MILESTONE_ALLOWED_COLUMNS: set[str] = {"name", "description", "deadline", "completed"}


# ---------------------------------------------------------------------------
# GET /api/projects/{project_id}/milestones
# ---------------------------------------------------------------------------


@router.get("/projects/{project_id}/milestones", response_model=list[MilestoneResponse])
@limiter.limit("60/minute")
async def list_milestones(
    request: Request,
    project_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(current_user_id_strict),
) -> list[MilestoneResponse]:
    """List milestones for a project."""
    async with db_pool.acquire() as conn:
        await _assert_project_owner(conn, project_id, user_id)

        rows = await conn.fetch(
            """SELECT * FROM milestones
               WHERE project_id = $1
                 AND user_id = $2
               ORDER BY deadline ASC NULLS LAST, created_at""",
            project_id,
            user_id,
        )
    return [MilestoneResponse(**dict(row)) for row in rows]


# ---------------------------------------------------------------------------
# GET /api/milestones/upcoming  (cross-project deadline feed)
# ---------------------------------------------------------------------------


@router.get("/milestones/upcoming", response_model=list[MilestoneDeadlineItem])
@limiter.limit("60/minute")
async def list_upcoming_milestones(
    request: Request,
    within_days: int = Query(default=7, ge=1, le=90),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(current_user_id_strict),
) -> list[MilestoneDeadlineItem]:
    """List the caller's incomplete, future milestones due within ``within_days``.

    Cross-project deadline feed (Telegram daily briefing). Scoped to the caller
    via ``m.user_id``; ``make_interval`` parameterises the window (no string
    concat).
    """
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT m.id, m.name, m.deadline, pr.name AS project_name
               FROM milestones m
               JOIN projects pr ON pr.id = m.project_id
               WHERE m.completed = FALSE
                 AND m.deadline > NOW()
                 AND m.deadline <= NOW() + make_interval(days => $1)
                 AND m.user_id = $2
               ORDER BY m.deadline""",
            within_days,
            user_id,
        )
    return [MilestoneDeadlineItem(**dict(row)) for row in rows]


# ---------------------------------------------------------------------------
# POST /api/projects/{project_id}/milestones
# ---------------------------------------------------------------------------


@router.post(
    "/projects/{project_id}/milestones",
    response_model=MilestoneResponse,
    status_code=201,
)
@limiter.limit("30/minute")
async def create_milestone(
    request: Request,
    project_id: int,
    body: MilestoneCreate,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(current_user_id_strict),
) -> MilestoneResponse:
    """Create a milestone in a project."""
    async with db_pool.acquire() as conn:
        await _assert_project_owner(conn, project_id, user_id)

        row = await conn.fetchrow(
            """
            INSERT INTO milestones (project_id, name, deadline, description, user_id)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING *
            """,
            project_id,
            body.name,
            body.deadline,
            body.description,
            user_id,
        )
    return MilestoneResponse(**dict(row))


# ---------------------------------------------------------------------------
# PUT /api/milestones/{milestone_id}
# ---------------------------------------------------------------------------


@router.put("/milestones/{milestone_id}", response_model=MilestoneResponse)
@limiter.limit("30/minute")
async def update_milestone(
    request: Request,
    milestone_id: int,
    body: MilestoneUpdate,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(current_user_id_strict),
) -> MilestoneResponse:
    """Update a milestone. Auto-sets completed_at when completed becomes True."""
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            existing = await conn.fetchrow(
                "SELECT * FROM milestones WHERE id = $1 AND user_id = $2 FOR UPDATE",
                milestone_id,
                user_id,
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


@router.delete("/milestones/{milestone_id}", status_code=204)
@limiter.limit("30/minute")
async def delete_milestone(
    request: Request,
    milestone_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(current_user_id_strict),
) -> None:
    """Delete a milestone."""
    await delete_or_404(
        db_pool,
        "DELETE FROM milestones WHERE id = $1 AND user_id = $2",
        milestone_id,
        user_id,
        detail="Milestone not found",
    )
    await log_audit(
        db_pool,
        action="delete",
        resource=f"milestone:{milestone_id}",
        user_id=str(user_id),
    )
