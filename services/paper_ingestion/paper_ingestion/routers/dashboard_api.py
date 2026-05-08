"""Dashboard API endpoints.

Provides aggregate metrics for the React frontend
(previously served by direct DB queries in the Streamlit dashboard).
"""

import logging

import asyncpg
from fastapi import APIRouter, Depends, Request
from jarvis_common import current_user_id_or_none

from paper_ingestion.deps import get_db_pool, limiter
from paper_ingestion.models import DashboardMetrics

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
    user_id = await current_user_id_or_none(request)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                (SELECT COUNT(*) FROM papers
                 WHERE user_id IS NOT DISTINCT FROM $1) AS total_papers,
                (SELECT COUNT(*) FROM papers p
                   WHERE p.user_id IS NOT DISTINCT FROM $1
                     AND NOT EXISTS (
                       SELECT 1 FROM paper_user_state pus
                        WHERE pus.paper_id = p.id
                          AND pus.user_id IS NOT DISTINCT FROM $1
                          AND COALESCE(pus.state, 'inbox') IN ('done','trash')
                     )) AS unread_papers,
                (SELECT COUNT(*) FROM papers p
                 LEFT JOIN paper_summaries ps ON p.id = ps.paper_id
                 WHERE p.user_id IS NOT DISTINCT FROM $1
                   AND ps.id IS NULL) AS pending_papers,
                -- TODO W2-4: cards/projects/topics/scheduled_nudges lack user_id columns today
                -- Add AND user_id IS NOT DISTINCT FROM $1 once Wave 3 migrations add those columns
                (SELECT COUNT(*) FROM cards
                 WHERE due_at IS NOT NULL AND due_at <= NOW()) AS due_cards,
                (SELECT COUNT(*) FROM projects
                 WHERE status = 'active') AS active_projects,
                (SELECT COUNT(*) FROM topics) AS topic_count,
                (SELECT COUNT(*) FROM scheduled_nudges) AS nudge_count
            """,
            user_id,
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
