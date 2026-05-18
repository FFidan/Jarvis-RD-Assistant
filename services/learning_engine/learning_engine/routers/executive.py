import asyncio
import datetime
from typing import Any

from asyncpg import Pool
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from jarvis_common.auth import current_user_id_strict, current_user_id_strict_with_owner_override
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


# --- /my-day query fragments -------------------------------------------------
# Each runs on its own pooled connection so the six independent reads can be
# issued concurrently via asyncio.gather (do NOT share one connection across
# gathered coroutines — asyncpg connections are not safe for concurrent use).

_TASKS_SQL = """
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
"""

_CARDS_DUE_SQL = "SELECT COUNT(*) FROM cards WHERE due_at <= NOW() AND user_id = $1"

_RECOMMENDATIONS_SQL = """
    SELECT pr.id as recommendation_id, pr.paper_id, pr.score, p.title, p.authors
    FROM paper_recommendations pr
    JOIN papers p ON pr.paper_id = p.id
    WHERE pr.dismissed = FALSE
      AND pr.user_id = $1
    ORDER BY pr.score DESC
    LIMIT $2
"""

_FOCUS_HOURS_SQL = (
    "SELECT COALESCE(focus_hours, 0) FROM daily_log WHERE log_date = CURRENT_DATE AND user_id = $1"
)

# Focus streak: consecutive days with focus_hours > 0, ending today or
# yesterday (gaps-and-islands). Behaviour-equivalent to the prior 365-row
# Python walk — CURRENT_DATE is UTC here (postgres container has no TZ set,
# matching the old datetime.now(UTC).date() anchor), and the run is counted
# only when its most recent qualifying day is CURRENT_DATE or CURRENT_DATE-1.
_FOCUS_STREAK_SQL = """
    WITH qualifying AS (
        SELECT DISTINCT log_date
        FROM daily_log
        WHERE focus_hours > 0 AND user_id = $1
    ),
    islands AS (
        SELECT log_date,
               log_date - (ROW_NUMBER() OVER (ORDER BY log_date))::int AS grp
        FROM qualifying
    ),
    latest_run AS (
        SELECT COUNT(*) AS len, MAX(log_date) AS last_day
        FROM islands
        GROUP BY grp
        ORDER BY last_day DESC
        LIMIT 1
    )
    SELECT COALESCE(
        (SELECT len FROM latest_run
          WHERE last_day >= CURRENT_DATE - INTERVAL '1 day'),
        0
    )::int
"""

_PROJECT_PULSE_SQL = """
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
"""


async def _fetch(pool: Pool, sql: str, *args: Any) -> list[Any]:
    async with pool.acquire() as conn:
        return await conn.fetch(sql, *args)


async def _fetchval(pool: Pool, sql: str, *args: Any) -> Any:
    async with pool.acquire() as conn:
        return await conn.fetchval(sql, *args)


async def _fetchrow(pool: Pool, sql: str, *args: Any) -> Any:
    async with pool.acquire() as conn:
        return await conn.fetchrow(sql, *args)


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
    """Fetch aggregated daily execution plan (tasks, cards, recommended papers).

    The six independent reads run concurrently, each on its own pooled
    connection. The focus streak is computed in SQL (gaps-and-islands) rather
    than walking up to 365 rows in Python — same value, no per-row transfer.
    """
    (
        tasks,
        cards_due,
        recommendations,
        today_focus_hours,
        focus_streak_days,
        project_pulse,
    ) = await asyncio.gather(
        _fetch(db_pool, _TASKS_SQL, user_id),
        _fetchval(db_pool, _CARDS_DUE_SQL, user_id),
        _fetch(db_pool, _RECOMMENDATIONS_SQL, user_id, limit_recommendations),
        _fetchval(db_pool, _FOCUS_HOURS_SQL, user_id),
        _fetchval(db_pool, _FOCUS_STREAK_SQL, user_id),
        _fetch(db_pool, _PROJECT_PULSE_SQL, user_id),
    )

    return MyDayResponse(
        tasks=[MyDayTaskItem.model_validate(dict(t)) for t in tasks],
        cards_due=int(cards_due or 0),
        recommendations=[MyDayRecommendationItem.model_validate(dict(r)) for r in recommendations],
        today_focus_hours=float(today_focus_hours or 0.0),
        focus_streak_days=int(focus_streak_days or 0),
        project_pulse=[MyDayProjectPulseItem.model_validate(dict(p)) for p in project_pulse],
    )


# --- /my-day-bundle: one round-trip superset of the ~11 My-Day page calls ----
# Each fragment re-queries the SAME tables the dedicated endpoints hit (intent
# → daily_intent, threads → thread, yesterday → tasks+daily_log, journal →
# journal_entries). Logic is mirrored from intent_repo / routers.threads /
# routers.my_day (paper_ingestion) — NOT imported, learning_engine owns its own
# pool and queries Postgres directly.

_INTENT_SQL = (
    "SELECT intent_text, updated_at FROM daily_intent "
    "WHERE user_id IS NOT DISTINCT FROM $1 AND intent_date = CURRENT_DATE"
)

_THREADS_SQL = (
    "SELECT id, title, anchor, progress, last_at, status, created_at "
    "FROM thread WHERE user_id = $1 AND status = 'open' "
    "ORDER BY last_at DESC"
)

_JOURNAL_TODAY_SQL = (
    "SELECT id, date, prompts, created_at, updated_at FROM journal_entries "
    "WHERE user_id = $1 AND date = CURRENT_DATE"
)

_YDAY_DONE_SQL = (
    "SELECT id, title, status FROM tasks "
    "WHERE user_id = $1 AND status = 'done' "
    "AND completed_at >= $2 AND completed_at < $3 "
    "ORDER BY completed_at"
)

_YDAY_DEFERRED_SQL = (
    "SELECT id, title, status FROM tasks "
    "WHERE user_id = $1 AND status IN ('todo', 'in_progress', 'blocked') "
    "AND (deadline IS NOT NULL AND deadline >= $2 AND deadline < $3) "
    "ORDER BY deadline"
)

_YDAY_LOG_SQL = (
    "SELECT focus_hours, cards_reviewed FROM daily_log WHERE user_id = $1 AND log_date = $2"
)


def _intent_payload(row: Any) -> dict[str, Any]:
    if not row:
        return {"intent": None, "updated_at": None}
    updated = row["updated_at"]
    return {
        "intent": row["intent_text"],
        "updated_at": updated.isoformat() if updated else None,
    }


def _thread_payload(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "anchor": row["anchor"],
        "progress": row["progress"],
        "last_at": row["last_at"].isoformat() if row["last_at"] else None,
        "status": row["status"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }


def _journal_payload(row: Any) -> dict[str, Any] | None:
    if not row:
        return None
    return {
        "id": row["id"],
        "date": row["date"].isoformat(),
        "prompts": row["prompts"] or {},
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


@router.get("/my-day-bundle")
@limiter.limit("60/minute")
async def get_my_day_bundle(
    request: Request,
    db_pool: Pool = Depends(get_db_pool),
    tz_offset_minutes: int = Query(
        0,
        ge=-840,
        le=840,
        description="Caller UTC offset in minutes east of UTC (JS -getTimezoneOffset()).",
    ),
    user_id: int = Depends(current_user_id_strict_with_owner_override),
) -> dict[str, Any]:
    """One-round-trip superset of the My-Day page's ~11 calls.

    Returns ``{tasks, intent, threads, yesterday, journal, pulse_today}``.
    ``pulse_today`` is always null here — the Pulse deck assembly lives in
    paper_ingestion and is not re-implementable in learning_engine without
    importing it; the frontend keeps its dedicated ``fetchPulseToday`` call as
    the source for that one section. Every field is null/empty tolerant.
    """
    # Yesterday window: local-midnight boundaries re-expressed as UTC instants
    # (mirrors routers.my_day.get_yesterday exactly).
    offset = datetime.timedelta(minutes=tz_offset_minutes)
    now_local = datetime.datetime.now(datetime.UTC) + offset
    yesterday_local_date = (now_local - datetime.timedelta(days=1)).date()
    start_utc = (
        datetime.datetime(
            yesterday_local_date.year,
            yesterday_local_date.month,
            yesterday_local_date.day,
            tzinfo=datetime.UTC,
        )
        - offset
    )
    end_utc = start_utc + datetime.timedelta(days=1)

    (
        tasks,
        intent_row,
        thread_rows,
        journal_row,
        yday_done,
        yday_deferred,
        yday_log,
    ) = await asyncio.gather(
        _fetch(db_pool, _TASKS_SQL, user_id),
        _fetchrow(db_pool, _INTENT_SQL, user_id),
        _fetch(db_pool, _THREADS_SQL, user_id),
        _fetchrow(db_pool, _JOURNAL_TODAY_SQL, user_id),
        _fetch(db_pool, _YDAY_DONE_SQL, user_id, start_utc, end_utc),
        _fetch(db_pool, _YDAY_DEFERRED_SQL, user_id, start_utc, end_utc),
        _fetchrow(db_pool, _YDAY_LOG_SQL, user_id, yesterday_local_date),
    )

    yesterday = {
        "date": yesterday_local_date.isoformat(),
        "focused_hours": float(yday_log["focus_hours"]) if yday_log else 0.0,
        "cards_reviewed": int(yday_log["cards_reviewed"]) if yday_log else 0,
        "tasks_done": len(yday_done),
        "completed": [
            {"id": r["id"], "title": r["title"], "status": r["status"]} for r in yday_done
        ],
        "deferred": [
            {"id": r["id"], "title": r["title"], "status": r["status"]} for r in yday_deferred
        ],
    }

    return {
        "tasks": [MyDayTaskItem.model_validate(dict(t)).model_dump(mode="json") for t in tasks],
        "intent": _intent_payload(intent_row),
        "threads": [_thread_payload(r) for r in thread_rows],
        "yesterday": yesterday,
        "journal": _journal_payload(journal_row),
        "pulse_today": None,
    }


@router.post("/tasks", status_code=201)
@limiter.limit("30/minute")
async def quick_add_task(
    request: Request,
    payload: QuickAddTaskRequest,
    db_pool: Pool = Depends(get_db_pool),
    user_id: int = Depends(current_user_id_strict),
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
