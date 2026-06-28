"""Recommendation feedback CRUD endpoints.

This module ships the read + bulk-delete API surface for the
``recommendation_feedback`` table. The router lives in its own module
because the prefix (``/api/recommendation_feedback``) does not fit under
``routers/papers.py``'s ``/api/papers`` prefix.
"""

from __future__ import annotations

import logging

import asyncpg
from fastapi import APIRouter, Depends, Query, Request
from jarvis_common.auth import get_current_user_id

from paper_ingestion.deps import get_db_pool, limiter
from paper_ingestion.models.papers import (
    DeleteFeedbackResponse,
    FeedbackListItem,
    FeedbackListResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/api/recommendation_feedback",
    tags=["recommendation_feedback"],
)


# ---------------------------------------------------------------------------
# GET /api/recommendation_feedback
# ---------------------------------------------------------------------------


@router.get("", response_model=FeedbackListResponse)
@limiter.limit("30/minute")
async def list_recommendation_feedback(
    request: Request,
    paper_id: int | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(get_current_user_id),
) -> FeedbackListResponse:
    """List recommendation_feedback rows for the current user.

    Joins ``papers`` (for ``title``) and ``topics`` (for ``topic_name``).
    Optional ``paper_id`` query param narrows results to a single paper.
    """
    _ = request  # required by @limiter.limit; not used in body
    async with db_pool.acquire() as conn:
        where_clauses = ["rf.user_id = $1"]
        params: list[object] = [user_id]
        if paper_id is not None:
            params.append(paper_id)
            where_clauses.append(f"rf.paper_id = ${len(params)}")
        where_sql = " AND ".join(where_clauses)
        rows = await conn.fetch(
            f"""SELECT rf.paper_id, p.title, rf.signal, rf.source, rf.reason,
                       rf.topic_id, t.name AS topic_name, rf.created_at
                  FROM recommendation_feedback rf
                  JOIN papers p ON p.id = rf.paper_id
                  LEFT JOIN topics t ON t.id = rf.topic_id
                 WHERE {where_sql}
                 ORDER BY rf.created_at DESC
                 LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}""",  # noqa: S608
            *params,
            limit,
            offset,
        )
        total = (
            await conn.fetchval(
                f"SELECT COUNT(*) FROM recommendation_feedback rf WHERE {where_sql}",  # noqa: S608
                *params,
            )
            or 0
        )
    items = [FeedbackListItem(**dict(row)) for row in rows]
    return FeedbackListResponse(items=items, total=int(total))


# ---------------------------------------------------------------------------
# DELETE /api/recommendation_feedback?topic_id=N
# ---------------------------------------------------------------------------


@router.delete("", response_model=DeleteFeedbackResponse)
@limiter.limit("5/minute")
async def delete_recommendation_feedback_by_topic(
    request: Request,
    topic_id: int = Query(...),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(get_current_user_id),
) -> DeleteFeedbackResponse:
    """Bulk-delete all recommendation_feedback rows for the given topic.

    Scoped to the current user (``user_id = $2``). Returns the number of
    rows deleted.
    """
    _ = request  # required by @limiter.limit; not used in body
    async with db_pool.acquire() as conn:
        result = await conn.execute(
            """DELETE FROM recommendation_feedback
                WHERE topic_id = $1
                  AND user_id = $2""",
            topic_id,
            user_id,
        )
        # asyncpg's execute() returns a status string like "DELETE 7".
        deleted = int(result.split()[1]) if result.startswith("DELETE") else 0
    return DeleteFeedbackResponse(deleted=deleted, topic_id=topic_id)
