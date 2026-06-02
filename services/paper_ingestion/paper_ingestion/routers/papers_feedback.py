"""Recommendation feedback endpoints: submit_feedback, delete_paper_feedback, trash_and_reject."""

import logging
from datetime import UTC, datetime

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from jarvis_common import ErrorResponse
from jarvis_common.auth import get_current_user_id
from jarvis_common.paper_state import trash_paper as _trash_paper

from paper_ingestion import papers_service
from paper_ingestion.deps import get_db_pool, limiter
from paper_ingestion.models import (
    FeedbackRequest,
    FeedbackResponse,
    MarkReadResponse,
)
from paper_ingestion.services.paper_state_helpers import _upsert_recommendation_feedback

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/api/papers",
    tags=["papers"],
    responses={
        400: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
)


@router.post("/{paper_id}/feedback", response_model=FeedbackResponse)
@limiter.limit("60/minute")
async def submit_feedback(
    request: Request,
    paper_id: int,
    body: FeedbackRequest,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(get_current_user_id),
) -> FeedbackResponse:
    """Record per-paper recommendation feedback.

    Writes to the ``recommendation_feedback`` table (one row per
    ``(paper_id, user_id, source)`` triple — repeat submissions overwrite
    the prior row).
    """
    async with db_pool.acquire() as conn:
        await papers_service.assert_paper_ownership(conn, paper_id, user_id)

        origin_row = await conn.fetchrow(
            "SELECT discovery_origin FROM papers WHERE id = $1",
            paper_id,
        )
        if origin_row is None:
            raise HTTPException(status_code=404, detail=f"Paper {paper_id} not found")
        if origin_row["discovery_origin"] == "user_initiated":
            raise HTTPException(
                status_code=400,
                detail=(
                    "recommendation feedback is only valid for system-discovered papers; "
                    "user_initiated papers are excluded from recommendation training"
                ),
            )

        try:
            await _upsert_recommendation_feedback(
                conn,
                paper_id,
                user_id,
                body.signal,
                body.source,
                body.reason,
            )
        except asyncpg.ForeignKeyViolationError as e:
            raise HTTPException(status_code=404, detail=f"Paper {paper_id} not found") from e
    return FeedbackResponse(
        paper_id=paper_id,
        signal=body.signal,
        source=body.source,
        created_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# DELETE /api/papers/{paper_id}/feedback  — clear a feedback signal
# ---------------------------------------------------------------------------


@router.delete("/{paper_id}/feedback", status_code=204)
@limiter.limit("60/minute")
async def delete_paper_feedback(
    request: Request,
    paper_id: int,
    source: str = Query(),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(get_current_user_id),
) -> None:
    """Delete a recommendation_feedback row for this paper+user+source triple.

    assert_paper_ownership is called before DELETE to prevent cross-user
    feedback deletion (IDOR protection).
    Idempotent — returns 204 regardless of whether a row was deleted.
    ``source`` must be supplied as a query parameter (e.g. ``?source=pulse_thumbs``).
    """
    async with db_pool.acquire() as conn:
        # Ownership check before DELETE — prevents cross-user feedback deletion.
        await papers_service.assert_paper_ownership(conn, paper_id, user_id)
        await conn.execute(
            "DELETE FROM recommendation_feedback"
            " WHERE paper_id = $1 AND user_id = $2 AND source = $3",
            paper_id,
            user_id,
            source,
        )


# ---------------------------------------------------------------------------
# PUT /api/papers/{paper_id}/trash_and_reject  — combined action (spec §4.4)
# ---------------------------------------------------------------------------


@router.put("/{paper_id}/trash_and_reject", response_model=MarkReadResponse)
@limiter.limit("30/minute")
async def trash_and_reject_paper(
    request: Request,
    paper_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(get_current_user_id),
):
    """Trash the paper AND record negative feedback (``source='dismiss_combined'``).

    Single transaction. The only combined action in the system per spec §4.4.
    """
    async with db_pool.acquire() as conn:
        await papers_service.assert_paper_ownership(conn, paper_id, user_id)
        async with conn.transaction():
            await _trash_paper(conn, paper_id, user_id)
            await _upsert_recommendation_feedback(
                conn,
                paper_id,
                user_id,
                "negative",
                "dismiss_combined",
            )
    return {"status": "ok", "paper_id": paper_id}
