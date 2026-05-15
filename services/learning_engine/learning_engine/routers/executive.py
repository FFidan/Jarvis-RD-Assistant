import datetime
from typing import Any

from asyncpg import Pool
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from jarvis_common.auth import current_user_id_strict_with_owner_override
from jarvis_common.paper_state import upsert_paper_user_state as _upsert_paper_user_state
from pydantic import BaseModel, Field

from learning_engine.deps import get_db_pool, limiter
from learning_engine.models import (
    FocusSessionResponse,
    MyDayProjectPulseItem,
    MyDayRecommendationItem,
    MyDayResponse,
    MyDayTaskItem,
)

router = APIRouter(prefix="/api/executive", tags=["executive"])


class FocusSessionRequest(BaseModel):
    duration_hours: float = Field(..., gt=0, le=24)
    task_id: int | None = None
    paper_id: int | None = None


class QuickAddTaskRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=500)
    project_id: int | None = None
    priority: int = Field(3, ge=1, le=4)


@router.get("/my-day", response_model=MyDayResponse)
@limiter.limit("60/minute")
async def get_my_day(
    request: Request,
    db_pool: Pool = Depends(get_db_pool),
    limit_recommendations: int = Query(3, ge=1, le=10),
    user_id: int = Depends(current_user_id_strict_with_owner_override),
) -> MyDayResponse:
    """Fetch aggregated daily execution plan (tasks, cards, recommended papers)."""
    async with db_pool.acquire() as conn:
        # Tasks: Todo (due today/overdue) + completed today, with project context.
        # WS-2D: scope by user_id (Wave-3B added tasks.user_id).
        tasks = await conn.fetch(
            """
            SELECT t.id, t.project_id, t.title, t.priority, t.deadline, t.status,
                   t.completed_at, p.name AS project_name, p.color AS project_color
            FROM tasks t
            LEFT JOIN projects p ON p.id = t.project_id
            WHERE t.user_id = $1
              AND ((t.status = 'todo'
                     AND (t.deadline IS NULL OR t.deadline < CURRENT_DATE + INTERVAL '1 day'))
                 OR (t.status = 'done' AND t.completed_at::date = CURRENT_DATE)
                 OR t.status IN ('in_progress', 'blocked'))
            ORDER BY t.status ASC, t.priority ASC, t.deadline ASC NULLS LAST
            LIMIT 20
            """,
            user_id,
        )

        # Flashcards due — WS-2D: scope by user_id (cards.user_id added in 070).
        cards_due = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM cards
            WHERE due_at <= NOW()
              AND user_id = $1
            """,
            user_id,
        )

        # Recommended papers
        recommendations = await conn.fetch(
            """
            SELECT pr.id as recommendation_id, pr.paper_id, pr.score, p.title, p.authors
            FROM paper_recommendations pr
            JOIN papers p ON pr.paper_id = p.id
            WHERE pr.dismissed = FALSE
              AND pr.user_id = $1
            ORDER BY pr.score DESC
            LIMIT $2
            """,
            user_id,
            limit_recommendations,
        )

        # Focus hours logged today
        today_focus_hours = (
            await conn.fetchval(
                "SELECT COALESCE(focus_hours, 0) FROM daily_log "
                "WHERE log_date = CURRENT_DATE AND user_id = $1",
                user_id,
            )
            or 0.0
        )

        # Focus streak: consecutive days with focus_hours > 0
        streak_rows = await conn.fetch(
            "SELECT log_date FROM daily_log "
            "WHERE focus_hours > 0 AND user_id = $1 "
            "ORDER BY log_date DESC LIMIT 365",
            user_id,
        )
        focus_streak_days = 0
        if streak_rows:
            today = datetime.datetime.now(datetime.UTC).date()
            # Allow streak to include today or start from yesterday
            expected = (
                today if streak_rows[0]["log_date"] == today else today - datetime.timedelta(days=1)
            )
            if streak_rows[0]["log_date"] == expected:
                for row in streak_rows:
                    if row["log_date"] == expected:
                        focus_streak_days += 1
                        expected -= datetime.timedelta(days=1)
                    else:
                        break

        # Project pulse: active projects with task progress and next milestone.
        # WS-2D: scope projects by owner (projects.user_id from Wave-3 / migration 064).
        project_pulse = await conn.fetch(
            """
            SELECT p.id, p.name, p.color,
                   COUNT(t.id) AS total_tasks,
                   COUNT(t.id) FILTER (WHERE t.status = 'done') AS done_tasks,
                   (SELECT m.name FROM milestones m
                    WHERE m.project_id = p.id AND m.completed = FALSE
                      AND m.user_id = $1
                    ORDER BY m.deadline ASC NULLS LAST LIMIT 1) AS next_milestone,
                   (SELECT m.deadline FROM milestones m
                    WHERE m.project_id = p.id AND m.completed = FALSE
                      AND m.user_id = $1
                    ORDER BY m.deadline ASC NULLS LAST LIMIT 1) AS next_milestone_deadline
            FROM projects p
            LEFT JOIN tasks t ON t.project_id = p.id
                AND t.user_id = $1
            WHERE p.status = 'active'
              AND p.user_id = $1
            GROUP BY p.id
            ORDER BY p.name
            """,
            user_id,
        )

    return MyDayResponse(
        tasks=[MyDayTaskItem.model_validate(dict(t)) for t in tasks],
        cards_due=int(cards_due or 0),
        recommendations=[MyDayRecommendationItem.model_validate(dict(r)) for r in recommendations],
        today_focus_hours=float(today_focus_hours),
        focus_streak_days=focus_streak_days,
        project_pulse=[MyDayProjectPulseItem.model_validate(dict(p)) for p in project_pulse],
    )


@router.post("/tasks", status_code=201)
@limiter.limit("30/minute")
async def quick_add_task(
    request: Request,
    payload: QuickAddTaskRequest,
    db_pool: Pool = Depends(get_db_pool),
    user_id: int = Depends(current_user_id_strict_with_owner_override),
) -> dict[str, Any]:
    """Quick-add a task, optionally linked to a project."""
    async with db_pool.acquire() as conn:
        if payload.project_id is not None:
            # WS-2D: scope project lookup by owner — IDOR otherwise.
            project_exists = await conn.fetchval(
                "SELECT id FROM projects WHERE id = $1 AND user_id = $2",
                payload.project_id,
                user_id,
            )
            if not project_exists:
                raise HTTPException(status_code=404, detail="Project not found")

        # WS-2D: write user_id (Wave-3B added the column but this insert never set it).
        row = await conn.fetchrow(
            "INSERT INTO tasks (title, project_id, priority, status, user_id) "
            "VALUES ($1, $2, $3, 'todo', $4) RETURNING *",
            payload.title,
            payload.project_id,
            payload.priority,
            user_id,
        )
    return dict(row)  # type: ignore[arg-type]


@router.post("/focus/log", response_model=FocusSessionResponse)
@limiter.limit("10/minute")
async def log_focus_session(
    request: Request,
    payload: FocusSessionRequest,
    db_pool: Pool = Depends(get_db_pool),
    user_id: int = Depends(current_user_id_strict_with_owner_override),
) -> FocusSessionResponse:
    """Log a completed focus session.

    Validation and mutations run inside a single transaction using SELECT FOR UPDATE
    to eliminate the TOCTOU race between existence checks and DML (LE-009).
    """
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            # Validate and lock referenced rows inside the transaction (LE-009: FOR UPDATE
            # prevents concurrent deletes from racing between the check and the DML).
            # WS-2D: scope by user_id to prevent IDOR — user A cannot log focus
            # against user B's task.
            if payload.task_id is not None:
                task_row = await conn.fetchrow(
                    "SELECT id FROM tasks WHERE id = $1 AND user_id = $2 FOR UPDATE",
                    payload.task_id,
                    user_id,
                )
                if task_row is None:
                    raise HTTPException(status_code=404, detail="Task not found")
            if payload.paper_id is not None:
                # Papers stay visible across users when NULL-owned (system papers).
                paper_row = await conn.fetchrow(
                    "SELECT id FROM papers WHERE id = $1 "
                    "AND (user_id IS NULL OR user_id IS NOT DISTINCT FROM $2) FOR UPDATE",
                    payload.paper_id,
                    user_id,
                )
                if paper_row is None:
                    raise HTTPException(status_code=404, detail="Paper not found")

            if payload.task_id is not None:
                await conn.execute(
                    "UPDATE tasks SET actual_hours = COALESCE(actual_hours, 0) + $1, "
                    "updated_at = NOW() WHERE id = $2 "
                    "AND user_id = $3",
                    payload.duration_hours,
                    payload.task_id,
                    user_id,
                )
            if payload.paper_id is not None:
                await _upsert_paper_user_state(
                    conn,
                    payload.paper_id,
                    user_id,
                    state="reading",
                    on_conflict="update_state_when_inbox_or_to_read",
                )
            await conn.execute(
                """INSERT INTO daily_log (user_id, log_date, focus_hours)
                VALUES ($1, CURRENT_DATE, $2)
                ON CONFLICT (user_id, log_date)
                DO UPDATE SET focus_hours = daily_log.focus_hours + $2""",
                user_id,
                payload.duration_hours,
            )

    return FocusSessionResponse(status="success", recorded_hours=payload.duration_hours)
