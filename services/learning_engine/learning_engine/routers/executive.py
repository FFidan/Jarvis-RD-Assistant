import datetime
from typing import Any

from asyncpg import Pool
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from jarvis_common.auth import current_user_id_or_none
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
) -> MyDayResponse:
    """Fetch aggregated daily execution plan (tasks, cards, recommended papers)."""
    async with db_pool.acquire() as conn:
        # Tasks: Todo (due today/overdue) + completed today, with project context
        tasks = await conn.fetch(
            """
            SELECT t.id, t.project_id, t.title, t.priority, t.deadline, t.status,
                   t.completed_at, p.name AS project_name, p.color AS project_color
            FROM tasks t
            LEFT JOIN projects p ON p.id = t.project_id
            WHERE (t.status = 'todo'
                   AND (t.deadline IS NULL OR t.deadline < CURRENT_DATE + INTERVAL '1 day'))
               OR (t.status = 'done' AND t.completed_at::date = CURRENT_DATE)
            ORDER BY t.status ASC, t.priority ASC, t.deadline ASC NULLS LAST
            LIMIT 20
            """
        )

        # Flashcards due
        cards_due = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM cards
            WHERE due_at <= NOW()
            """
        )

        # Recommended papers
        recommendations = await conn.fetch(
            """
            SELECT pr.id as recommendation_id, pr.paper_id, pr.score, p.title, p.authors
            FROM paper_recommendations pr
            JOIN papers p ON pr.paper_id = p.id
            WHERE pr.dismissed = FALSE
            ORDER BY pr.score DESC
            LIMIT $1
            """,
            limit_recommendations,
        )

        # Focus hours logged today
        today_focus_hours = (
            await conn.fetchval(
                "SELECT COALESCE(focus_hours, 0) FROM daily_log WHERE log_date = CURRENT_DATE"
            )
            or 0.0
        )

        # Focus streak: consecutive days with focus_hours > 0
        streak_rows = await conn.fetch(
            "SELECT log_date FROM daily_log WHERE focus_hours > 0 ORDER BY log_date DESC LIMIT 365"
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

        # Project pulse: active projects with task progress and next milestone
        project_pulse = await conn.fetch("""
            SELECT p.id, p.name, p.color,
                   COUNT(t.id) AS total_tasks,
                   COUNT(t.id) FILTER (WHERE t.status = 'done') AS done_tasks,
                   (SELECT m.name FROM milestones m
                    WHERE m.project_id = p.id AND m.completed = FALSE
                    ORDER BY m.deadline ASC NULLS LAST LIMIT 1) AS next_milestone,
                   (SELECT m.deadline FROM milestones m
                    WHERE m.project_id = p.id AND m.completed = FALSE
                    ORDER BY m.deadline ASC NULLS LAST LIMIT 1) AS next_milestone_deadline
            FROM projects p
            LEFT JOIN tasks t ON t.project_id = p.id
            WHERE p.status = 'active'
            GROUP BY p.id
            ORDER BY p.name
        """)

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
) -> dict[str, Any]:
    """Quick-add a task, optionally linked to a project."""
    async with db_pool.acquire() as conn:
        if payload.project_id is not None:
            project_exists = await conn.fetchval(
                "SELECT id FROM projects WHERE id = $1",
                payload.project_id,
            )
            if not project_exists:
                raise HTTPException(status_code=404, detail="Project not found")

        row = await conn.fetchrow(
            "INSERT INTO tasks (title, project_id, priority, status) "
            "VALUES ($1, $2, $3, 'todo') RETURNING *",
            payload.title,
            payload.project_id,
            payload.priority,
        )
    return dict(row)  # type: ignore[arg-type]


@router.post("/focus/log", response_model=FocusSessionResponse)
@limiter.limit("10/minute")
async def log_focus_session(
    request: Request,
    payload: FocusSessionRequest,
    db_pool: Pool = Depends(get_db_pool),
) -> FocusSessionResponse:
    """Log a completed focus session.

    Validation and mutations run inside a single transaction using SELECT FOR UPDATE
    to eliminate the TOCTOU race between existence checks and DML (LE-009).
    """
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            # Validate and lock referenced rows inside the transaction (LE-009: FOR UPDATE
            # prevents concurrent deletes from racing between the check and the DML).
            if payload.task_id is not None:
                task_row = await conn.fetchrow(
                    "SELECT id FROM tasks WHERE id = $1 FOR UPDATE",
                    payload.task_id,
                )
                if task_row is None:
                    raise HTTPException(status_code=404, detail="Task not found")
            if payload.paper_id is not None:
                paper_row = await conn.fetchrow(
                    "SELECT id FROM papers WHERE id = $1 FOR UPDATE",
                    payload.paper_id,
                )
                if paper_row is None:
                    raise HTTPException(status_code=404, detail="Paper not found")

            if payload.task_id is not None:
                await conn.execute(
                    "UPDATE tasks SET actual_hours = COALESCE(actual_hours, 0) + $1, "
                    "updated_at = NOW() WHERE id = $2",
                    payload.duration_hours,
                    payload.task_id,
                )
            if payload.paper_id is not None:
                user_id = await current_user_id_or_none(request)
                await conn.execute(
                    """INSERT INTO paper_user_state (paper_id, user_id, state)
                       VALUES ($1, $2, 'reading')
                       ON CONFLICT (paper_id, user_id) DO UPDATE
                          SET state = 'reading'
                        WHERE paper_user_state.state IN ('inbox', 'to_read')""",
                    payload.paper_id,
                    user_id,
                )
            # Atomic upsert — ON CONFLICT handles concurrent inserts safely.
            await conn.execute(
                """INSERT INTO daily_log (log_date, focus_hours)
                VALUES (CURRENT_DATE, $1)
                ON CONFLICT (log_date) DO UPDATE SET focus_hours = daily_log.focus_hours + $1""",
                payload.duration_hours,
            )

    return FocusSessionResponse(status="success", recorded_hours=payload.duration_hours)
