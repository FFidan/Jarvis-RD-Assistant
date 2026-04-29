"""Dashboard API endpoints.

Provides aggregate metrics and user-state CRUD that the React frontend
needs (previously served by direct DB queries in the Streamlit dashboard).
"""

import logging

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request
from jarvis_common import assert_paper_ownership
from jarvis_common import jobs as jobs_lib
from jarvis_common.auth import current_user_id_or_none

from paper_ingestion.deps import get_db_pool, limiter
from paper_ingestion.models import DashboardMetrics, UserStateResponse, UserStateUpsert

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["dashboard"])


# ---------------------------------------------------------------------------
# GET /api/dashboard/metrics
# ---------------------------------------------------------------------------


@router.get("/dashboard/metrics", response_model=DashboardMetrics)
@limiter.limit("60/minute")
async def get_dashboard_metrics(
    request: Request,
    pool: asyncpg.Pool = Depends(get_db_pool),  # type: ignore[type-arg]
) -> DashboardMetrics:
    """Return aggregate counts for the dashboard home page.

    Mirrors the SQL previously embedded in ``dashboard/app.py``.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                (SELECT COUNT(*) FROM papers) AS total_papers,
                (SELECT COUNT(*) FROM papers p
                   WHERE NOT EXISTS (
                     SELECT 1 FROM paper_user_state pus
                   WHERE pus.paper_id = p.id
                     AND (
                       pus.status = 'read'
                       OR COALESCE(pus.archived, FALSE)
                       OR pus.status = 'archived'
                     )
                 )) AS unread_papers,
                (SELECT COUNT(*) FROM papers p
                 LEFT JOIN paper_summaries ps ON p.id = ps.paper_id
                 WHERE ps.id IS NULL) AS pending_papers,
                (SELECT COUNT(*) FROM cards
                 WHERE due_at IS NOT NULL AND due_at <= NOW()) AS due_cards,
                (SELECT COUNT(*) FROM projects
                 WHERE status = 'active') AS active_projects,
                (SELECT COUNT(*) FROM topics) AS topic_count,
                (SELECT COUNT(*) FROM scheduled_nudges) AS nudge_count
            """
        )

    if not row:
        return DashboardMetrics(
            total_papers=0,
            unread_papers=0,
            pending_papers=0,
            due_cards=0,
            active_projects=0,
            topic_count=0,
            nudge_count=0,
            onboarding_stage="needs_topics",
        )

    # Derive onboarding stage from aggregate counts
    if row["topic_count"] == 0:
        onboarding_stage = "needs_topics"
    elif row["total_papers"] == 0:
        onboarding_stage = "needs_papers"
    elif row["pending_papers"] >= row["total_papers"]:
        onboarding_stage = "needs_processing"
    else:
        onboarding_stage = "complete"

    return DashboardMetrics(
        total_papers=row["total_papers"],
        unread_papers=row["unread_papers"],
        pending_papers=row["pending_papers"],
        due_cards=row["due_cards"],
        active_projects=row["active_projects"],
        topic_count=row["topic_count"],
        nudge_count=row["nudge_count"],
        onboarding_stage=onboarding_stage,
    )


# ---------------------------------------------------------------------------
# PUT /api/papers/{paper_id}/user-state
# ---------------------------------------------------------------------------


@router.put("/papers/{paper_id}/user-state", response_model=UserStateResponse)
@limiter.limit("30/minute")
async def upsert_user_state(
    request: Request,
    paper_id: int,
    body: UserStateUpsert,
    pool: asyncpg.Pool = Depends(get_db_pool),  # type: ignore[type-arg]
) -> UserStateResponse:
    """Create or update per-paper user state (status, rating, notes, flag).

    Uses ``INSERT ... ON CONFLICT DO UPDATE`` so partial updates work:
    only non-null fields in the request body overwrite existing values.
    """
    user_id = await current_user_id_or_none(request)
    async with pool.acquire() as conn:
        await assert_paper_ownership(conn, paper_id, user_id)
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO paper_user_state
                    (paper_id, user_id, status, starred, archived,
                     preference, rating, user_notes, flagged)
                VALUES ($1, $2, $3, COALESCE($4, FALSE), COALESCE($5, FALSE),
                        COALESCE($6, 'none'), $7, $8, COALESCE($9, FALSE))
                ON CONFLICT (paper_id, user_id) DO UPDATE SET
                    status     = COALESCE($3, paper_user_state.status),
                    starred    = COALESCE($4, paper_user_state.starred),
                    archived   = COALESCE($5, paper_user_state.archived),
                    preference = COALESCE($6, paper_user_state.preference),
                    rating     = COALESCE($7, paper_user_state.rating),
                    user_notes = COALESCE($8, paper_user_state.user_notes),
                    flagged    = COALESCE($9, paper_user_state.flagged)
                RETURNING status, starred, archived, preference, rating, user_notes, flagged
                """,
                paper_id,
                user_id,
                body.status,
                body.starred,
                body.archived,
                body.preference,
                body.rating,
                body.user_notes,
                body.flagged,
            )
        except asyncpg.ForeignKeyViolationError as e:
            raise HTTPException(status_code=404, detail=f"Paper {paper_id} not found") from e

    if not row:  # pragma: no cover — defensive
        raise HTTPException(status_code=500, detail="Upsert returned no row")

    # Trigger Zotero push when a paper is newly starred and has project links.
    # The job handler checks Zotero config at runtime and returns early if disabled.
    if body.status == "starred" or body.starred is True:
        try:
            async with pool.acquire() as conn:
                project_count = await conn.fetchval(
                    "SELECT COUNT(*) FROM project_papers WHERE paper_id = $1",
                    paper_id,
                )
            if project_count:
                await jobs_lib.enqueue(pool, "zotero.push", {"paper_id": paper_id})
                logger.debug("Enqueued zotero.push for starred paper %d", paper_id)
        except Exception:
            logger.warning(
                "Failed to enqueue zotero.push after star for paper %d",
                paper_id,
                exc_info=True,
            )

    return UserStateResponse(
        status=row["status"],
        starred=bool(row["starred"]),
        archived=bool(row["archived"]),
        preference=row["preference"] or "none",
        rating=row["rating"],
        user_notes=row["user_notes"],
        flagged=bool(row["flagged"]),
    )
