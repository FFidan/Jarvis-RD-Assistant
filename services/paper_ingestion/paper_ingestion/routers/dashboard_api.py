"""Dashboard API endpoints.

Provides aggregate metrics for the React frontend
(previously served by direct DB queries in the Streamlit dashboard).
"""

import logging

import asyncpg
from fastapi import APIRouter, Depends, Request
from jarvis_common import current_user_id_strict

from paper_ingestion.deps import get_db_pool, limiter
from paper_ingestion.models import DashboardMetrics
from paper_ingestion.queries.predicates import EXCLUDED_STATE_SQL

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
    user_id: int = Depends(current_user_id_strict),
) -> DashboardMetrics:
    """Return aggregate counts for the dashboard home page.

    Mirrors the SQL previously embedded in ``dashboard/app.py``.
    """
    async with pool.acquire() as conn:
        # Paper-count metrics scope through ``user_library`` (the caller's
        # library) instead of the legacy ``papers.user_id`` predicate.
        # In single-user mode (user_id=None) the user_library JOIN matches
        # nothing — so we keep the legacy SELECT shape (no library JOIN).
        if user_id is not None:
            row = await conn.fetchrow(
                f"""
                SELECT
                    (SELECT COUNT(*) FROM user_library
                     WHERE user_id = $1) AS total_papers,
                    (SELECT COUNT(*) FROM user_library ul
                       WHERE ul.user_id = $1
                         AND NOT EXISTS (
                           SELECT 1 FROM paper_user_state pus
                            WHERE pus.paper_id = ul.paper_id
                              AND pus.user_id = $1
                              AND {EXCLUDED_STATE_SQL}
                         )) AS unread_papers,
                    (SELECT COUNT(*) FROM user_library ul
                     LEFT JOIN paper_summaries ps ON ul.paper_id = ps.paper_id
                     WHERE ul.user_id = $1
                       AND ps.id IS NULL) AS pending_papers,
                    (SELECT COUNT(*) FROM cards
                     WHERE due_at IS NOT NULL AND due_at <= NOW()
                       AND user_id = $1) AS due_cards,
                    (SELECT COUNT(*) FROM projects
                     WHERE status = 'active'
                       AND user_id IS NOT DISTINCT FROM $1) AS active_projects,
                    (SELECT COUNT(*) FROM topics) AS topic_count,
                    (SELECT COUNT(*) FROM scheduled_nudges) AS nudge_count
                """,
                user_id,
            )
        else:
            row = await conn.fetchrow(
                f"""
                SELECT
                    (SELECT COUNT(*) FROM papers) AS total_papers,
                    (SELECT COUNT(*) FROM papers p
                       WHERE NOT EXISTS (
                           SELECT 1 FROM paper_user_state pus
                            WHERE pus.paper_id = p.id
                              AND pus.user_id IS NULL
                              AND {EXCLUDED_STATE_SQL}
                         )) AS unread_papers,
                    (SELECT COUNT(*) FROM papers p
                     LEFT JOIN paper_summaries ps ON p.id = ps.paper_id
                     WHERE ps.id IS NULL) AS pending_papers,
                    (SELECT COUNT(*) FROM cards
                     WHERE due_at IS NOT NULL AND due_at <= NOW()
                       AND user_id IS NULL) AS due_cards,
                    (SELECT COUNT(*) FROM projects
                     WHERE status = 'active'
                       AND user_id IS NULL) AS active_projects,
                    (SELECT COUNT(*) FROM topics) AS topic_count,
                    (SELECT COUNT(*) FROM scheduled_nudges) AS nudge_count
                """,
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
