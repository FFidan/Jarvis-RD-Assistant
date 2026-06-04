"""Paper detail and batch-save endpoints: get_paper_detail, batch_save_papers."""

import logging
import uuid
from typing import Annotated
from urllib.parse import urlparse

import asyncpg
from fastapi import APIRouter, Body, Depends, HTTPException, Request
from jarvis_common import ErrorResponse
from jarvis_common.auth import get_current_user_id
from jarvis_common.library import add_to_library

from paper_ingestion import papers_service
from paper_ingestion.converters import (
    row_to_chunk_response,
    row_to_paper_response,
    row_to_summary_response,
)
from paper_ingestion.deps import get_db_pool, limiter
from paper_ingestion.models import (
    PaperCreate,
    PaperDetailResponse,
    PaperResponse,
    RecentFeedback,
    UserStateResponse,
)
from paper_ingestion.pdf_processor import ALLOWED_PDF_DOMAINS
from paper_ingestion.services.pdf_workflow import upsert_paper

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


# ---------------------------------------------------------------------------
# GET /api/papers/{paper_id}
# ---------------------------------------------------------------------------


@router.get("/{paper_id}", response_model=PaperDetailResponse)
@limiter.limit("60/minute")
async def get_paper_detail(
    request: Request,
    paper_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(get_current_user_id),
) -> PaperDetailResponse:
    """Get a paper with its summary, chunks, user state, and most recent feedback."""
    async with db_pool.acquire() as conn:
        await papers_service.assert_paper_ownership(conn, paper_id, user_id)
        paper_row = await conn.fetchrow("SELECT * FROM papers WHERE id = $1", paper_id)
        if not paper_row:
            raise HTTPException(status_code=404, detail="Paper not found")

        summary_row = await conn.fetchrow(
            "SELECT * FROM paper_summaries WHERE paper_id = $1", paper_id
        )
        chunk_rows = await conn.fetch(
            "SELECT * FROM paper_chunks WHERE paper_id = $1 ORDER BY chunk_index", paper_id
        )
        user_state_row = await conn.fetchrow(
            """SELECT COALESCE(state, 'inbox') AS state,
                      state_before_trash,
                      COALESCE(starred, FALSE) AS starred,
                      rating, user_notes,
                      COALESCE(flagged, FALSE) AS flagged,
                      updated_at
               FROM paper_user_state
               WHERE paper_id = $1 AND user_id = $2
               LIMIT 1""",
            paper_id,
            user_id,
        )
        feedback_row = await conn.fetchrow(
            """SELECT signal, source, created_at
               FROM recommendation_feedback
               WHERE paper_id = $1 AND user_id = $2
               ORDER BY created_at DESC LIMIT 1""",
            paper_id,
            user_id,
        )
        # PI-SEC-01: scope the link count to the caller's own projects. Without
        # the projects JOIN this counted other users' links too, leaking a
        # cross-tenant signal (gates the "Send to Zotero" button). NULL-safe
        # predicate keeps single-user mode (user_id IS NULL) counting all rows.
        project_link_count = await conn.fetchval(
            """SELECT COUNT(*)
               FROM project_papers pp
               JOIN projects pr ON pr.id = pp.project_id
                 AND ($2::bigint IS NULL OR pr.user_id IS NOT DISTINCT FROM $2)
               WHERE pp.paper_id = $1""",
            paper_id,
            user_id,
        )
        last_process_job_status = await conn.fetchval(
            """
            SELECT CASE pj.status
                     WHEN 'failed' THEN 'failed'
                     ELSE 'other' END
            FROM procrastinate_jobs pj
            WHERE pj.task_name IN ('paper.process', 'paper.analyze')
              AND pj.args->>'paper_id' = $1::text
              AND pj.args->>'user_id' = $2::text
            ORDER BY pj.id DESC
            LIMIT 1
            """,
            str(paper_id),
            str(user_id),
        )

    paper = row_to_paper_response(paper_row)
    summary = row_to_summary_response(summary_row) if summary_row else None
    chunks = [row_to_chunk_response(r) for r in chunk_rows]
    user_state = (
        UserStateResponse(
            state=user_state_row["state"],
            state_before_trash=user_state_row["state_before_trash"],
            starred=bool(user_state_row["starred"]),
            rating=user_state_row["rating"],
            user_notes=user_state_row["user_notes"],
            flagged=bool(user_state_row["flagged"]),
            updated_at=user_state_row["updated_at"],
        )
        if user_state_row
        else None
    )
    recent_feedback = (
        RecentFeedback(
            signal=feedback_row["signal"],
            source=feedback_row["source"],
            created_at=feedback_row["created_at"],
        )
        if feedback_row
        else None
    )
    has_project_links = bool(project_link_count)
    processing_failed = last_process_job_status == "failed"

    return PaperDetailResponse(
        paper=paper,
        summary=summary,
        chunks=chunks,
        user_state=user_state,
        recent_feedback=recent_feedback,
        has_project_links=has_project_links,
        processing_failed=processing_failed,
    )


# ---------------------------------------------------------------------------
# POST /api/papers/batch-save
# ---------------------------------------------------------------------------


@router.post("/batch-save", response_model=list[PaperResponse])
@limiter.limit("5/minute")
async def batch_save_papers(
    request: Request,
    papers: Annotated[list[PaperCreate], Body()],
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(get_current_user_id),
) -> list[PaperResponse]:
    """Upsert a list of papers to the database (by external_id).

    pdf_url is validated against ALLOWED_PDF_DOMAINS at persistence time;
    non-allowlisted URLs are cleared before upsert.
    """
    max_batch = 100
    if len(papers) > max_batch:
        raise HTTPException(400, f"Batch size cannot exceed {max_batch}")
    if not papers:
        return []
    results: list[PaperResponse] = []
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            for paper in papers:
                # Validate pdf_url against ALLOWED_PDF_DOMAINS; clear non-allowlisted URLs.
                if paper.pdf_url is not None:
                    try:
                        parsed = urlparse(str(paper.pdf_url))
                        hostname = parsed.hostname or ""
                        if (
                            parsed.scheme not in ("http", "https")
                            or hostname not in ALLOWED_PDF_DOMAINS
                        ):
                            paper.pdf_url = None  # type: ignore[assignment]
                    except Exception:
                        paper.pdf_url = None  # type: ignore[assignment]
                # Stamp citation_batch origin
                paper.discovery_origin = "citation_batch"
                row = await upsert_paper(conn, paper, discovered_by=user_id)
                if user_id is not None:
                    await add_to_library(
                        conn,
                        user_id=user_id,
                        paper_id=row["id"],
                        added_via="batch_save",
                    )
                results.append(row_to_paper_response(row))
    for saved in results:
        try:
            from jarvis_common.task_registry import KIND_TO_TASK  # noqa: PLC0415

            await KIND_TO_TASK["paper.analyze"].defer_async(
                job_id=str(uuid.uuid4()), user_id=user_id, paper_id=saved.id
            )
        except Exception:
            logger.exception("paper.analyze enqueue failed for paper %d", saved.id)
    return results
