"""Paper CRUD and metadata endpoints."""

import logging
from typing import Annotated

import asyncpg
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from jarvis_common import ErrorResponse, assert_paper_ownership, escape_like
from jarvis_common.auth import current_user_id_or_none

from paper_ingestion.converters import (
    row_to_chunk_response,
    row_to_paper_response,
    row_to_summary_response,
)
from paper_ingestion.deps import get_db_pool, get_optional_embedder, limiter
from paper_ingestion.models import (
    FeedbackRequest,
    FeedbackResponse,
    MarkReadResponse,
    PaperBriefResponse,
    PaperCreate,
    PaperDetailResponse,
    PaperResponse,
    PaperStatus,
    SourceType,
    UserStateResponse,
)
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
# GET /api/papers/brief
# ---------------------------------------------------------------------------


@router.get("/brief", response_model=list[PaperBriefResponse])
@limiter.limit("60/minute")
async def list_papers_brief(
    request: Request,
    search: str | None = Query(default=None, min_length=1),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> list[dict]:
    """Return lightweight paper list for selector dropdowns."""
    async with db_pool.acquire() as conn:
        if search:
            rows = await conn.fetch(
                """SELECT id, title, source_type, published_date
                   FROM papers
                   WHERE title ILIKE '%' || $1 || '%' ESCAPE '\\'
                   ORDER BY created_at DESC
                   LIMIT 200""",
                escape_like(search),
            )
        else:
            rows = await conn.fetch(
                """SELECT id, title, source_type, published_date
                   FROM papers
                   ORDER BY created_at DESC
                   LIMIT 200"""
            )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# GET /api/papers
# ---------------------------------------------------------------------------


@router.get("", response_model=list[PaperResponse])
@limiter.limit("60/minute")
async def list_papers(
    request: Request,
    status: PaperStatus | None = None,
    source_type: SourceType | None = None,
    topic_id: int | None = None,
    q: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    embedder=Depends(get_optional_embedder),
) -> list[PaperResponse]:
    """List papers with optional filters.

    When a search query ``q`` is provided, attempts hybrid BM25 + semantic
    search via Reciprocal Rank Fusion.  Falls back to BM25-only if the
    embedder or Qdrant is unavailable.

    Parameters
    ----------
    status : PaperStatus | None
        Filter by user state status.
    source_type : SourceType | None
        Filter by paper source.
    topic_id : int | None
        Filter by associated topic.
    q : str | None
        Search query (triggers hybrid search when set).
    limit, offset : int
        Pagination parameters.

    Returns
    -------
    list[PaperResponse]
        Matching papers ordered by relevance (when searching) or
        ``created_at DESC``.
    """
    from paper_ingestion.converters import hybrid_dict_to_paper_response

    # ------------------------------------------------------------------
    # Hybrid search path: q is set and no other filters are active
    # ------------------------------------------------------------------
    has_extra_filters = any([status, source_type, topic_id])
    if q and not has_extra_filters:
        if embedder is not None and embedder.qdrant is not None:
            try:
                hybrid_results = await embedder.hybrid_search(
                    q, db_pool, limit=limit, offset=offset
                )
                return [await hybrid_dict_to_paper_response(r, db_pool) for r in hybrid_results]
            except Exception:
                logger.warning(
                    "Hybrid search failed, falling back to BM25-only",
                    exc_info=True,
                )

    # ------------------------------------------------------------------
    # Standard / fallback BM25 query path
    # ------------------------------------------------------------------
    query = "SELECT p.* FROM papers p"
    joins: list[str] = []
    conditions: list[str] = []
    params: list = []

    if topic_id is not None:
        joins.append("JOIN paper_topics pt ON p.id = pt.paper_id")
        params.append(topic_id)
        conditions.append(f"pt.topic_id = ${len(params)}")

    if status is not None:
        if status.value == "new":
            # Papers without a user_state row are implicitly "new"
            joins.append("LEFT JOIN paper_user_state pus ON p.id = pus.paper_id")
            params.append(status.value)
            conditions.append(f"(pus.status = ${len(params)} OR pus.paper_id IS NULL)")
        else:
            joins.append("JOIN paper_user_state pus ON p.id = pus.paper_id")
            params.append(status.value)
            conditions.append(f"pus.status = ${len(params)}")

    if source_type is not None:
        params.append(source_type.value)
        conditions.append(f"p.source_type = ${len(params)}")

    if q:
        params.append(q)
        conditions.append(f"p.search_vector @@ plainto_tsquery('english', ${len(params)})")

    if joins:
        query += " " + " ".join(joins)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)

    params.extend([limit, offset])
    query += f" ORDER BY p.created_at DESC LIMIT ${len(params) - 1} OFFSET ${len(params)}"

    async with db_pool.acquire() as conn:
        rows = await conn.fetch(query, *params)

    return [row_to_paper_response(row) for row in rows]


# ---------------------------------------------------------------------------
# GET /api/papers/{paper_id}
# ---------------------------------------------------------------------------


@router.get("/{paper_id}", response_model=PaperDetailResponse)
@limiter.limit("60/minute")
async def get_paper_detail(
    request: Request,
    paper_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> PaperDetailResponse:
    """Get a paper with its summary and chunks.

    Parameters
    ----------
    paper_id : int
        Database paper ID.

    Returns
    -------
    PaperDetailResponse
        Paper, optional summary, and all chunks.
    """
    user_id = await current_user_id_or_none(request)
    async with db_pool.acquire() as conn:
        await assert_paper_ownership(conn, paper_id, user_id)
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
            "SELECT status, rating, user_notes, flagged FROM paper_user_state WHERE paper_id = $1",
            paper_id,
        )
        project_link_count = await conn.fetchval(
            "SELECT COUNT(*) FROM project_papers WHERE paper_id = $1",
            paper_id,
        )

    paper = row_to_paper_response(paper_row)
    summary = row_to_summary_response(summary_row) if summary_row else None
    chunks = [row_to_chunk_response(r) for r in chunk_rows]
    user_state = (
        UserStateResponse(
            status=user_state_row["status"],
            rating=user_state_row["rating"],
            user_notes=user_state_row["user_notes"],
            flagged=user_state_row["flagged"],
        )
        if user_state_row
        else None
    )
    has_project_links = bool(project_link_count)

    return PaperDetailResponse(
        paper=paper,
        summary=summary,
        chunks=chunks,
        user_state=user_state,
        has_project_links=has_project_links,
    )


# ---------------------------------------------------------------------------
# PUT /api/papers/{paper_id}/read
# ---------------------------------------------------------------------------


@router.put("/{paper_id}/read", response_model=MarkReadResponse)
@limiter.limit("60/minute")
async def mark_paper_read(
    request: Request,
    paper_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
):
    """Mark a paper as read.

    Parameters
    ----------
    request : Request
        FastAPI request (needed by rate limiter).
    paper_id : int
        Database paper ID.
    db_pool : asyncpg.Pool
        Injected database pool.

    Returns
    -------
    dict
        ``{"status": "ok", "paper_id": <id>}``
    """
    user_id = await current_user_id_or_none(request)
    async with db_pool.acquire() as conn:
        await assert_paper_ownership(conn, paper_id, user_id)
        row = await conn.fetchrow(
            "SELECT id FROM papers WHERE id = $1",
            paper_id,
        )
        if not row:
            raise HTTPException(status_code=404, detail="Paper not found")
        await conn.execute(
            """INSERT INTO paper_user_state (paper_id, status)
               VALUES ($1, 'read')
               ON CONFLICT (paper_id) DO UPDATE SET status = 'read'""",
            paper_id,
        )
    return {"status": "ok", "paper_id": paper_id}


# ---------------------------------------------------------------------------
# PUT /api/papers/{paper_id}/bookmark
# ---------------------------------------------------------------------------


@router.put("/{paper_id}/bookmark", response_model=MarkReadResponse)
@limiter.limit("30/minute")
async def bookmark_paper(
    request: Request,
    paper_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
):
    """Toggle bookmark (star) state for a paper.

    Parameters
    ----------
    request : Request
        FastAPI request (needed by rate limiter).
    paper_id : int
        Database paper ID.
    db_pool : asyncpg.Pool
        Injected database pool.

    Returns
    -------
    dict
        ``{"status": "ok", "paper_id": <id>}``
    """
    user_id = await current_user_id_or_none(request)
    async with db_pool.acquire() as conn:
        await assert_paper_ownership(conn, paper_id, user_id)
        row = await conn.fetchrow(
            "SELECT id FROM papers WHERE id = $1",
            paper_id,
        )
        if not row:
            raise HTTPException(status_code=404, detail="Paper not found")
        await conn.execute(
            """INSERT INTO paper_user_state (paper_id, status)
               VALUES ($1, 'starred')
               ON CONFLICT (paper_id) DO UPDATE SET status = 'starred'""",
            paper_id,
        )
    return {"status": "ok", "paper_id": paper_id}


# ---------------------------------------------------------------------------
# POST /api/papers/batch-save
# ---------------------------------------------------------------------------


@router.post("/batch-save", response_model=list[PaperResponse])
@limiter.limit("5/minute")
async def batch_save_papers(
    request: Request,
    papers: Annotated[list[PaperCreate], Body()],
    db_pool: asyncpg.Pool = Depends(get_db_pool),
) -> list[PaperResponse]:
    """Upsert a list of papers to the database (by external_id)."""
    max_batch = 100
    if len(papers) > max_batch:
        raise HTTPException(400, f"Batch size cannot exceed {max_batch}")
    if not papers:
        return []
    results: list[PaperResponse] = []
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            for paper in papers:
                row = await upsert_paper(conn, paper)
                results.append(row_to_paper_response(row))
    return results


# ---------------------------------------------------------------------------
# POST /api/papers/{paper_id}/feedback
# ---------------------------------------------------------------------------


@router.post("/{paper_id}/feedback", response_model=FeedbackResponse)
@limiter.limit("30/minute")
async def submit_feedback(
    request: Request,
    paper_id: int,
    feedback: FeedbackRequest,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
):
    """Allow users to rate a paper (1-5) and/or flag suspicious summaries.

    Both fields live in the JSON body (see :class:`FeedbackRequest`); at least
    one of ``rating`` / ``flagged`` must be provided or the handler returns 400.
    """
    rating = feedback.rating
    flagged = feedback.flagged
    if rating is None and flagged is None:
        raise HTTPException(
            status_code=400,
            detail="At least one of 'rating' or 'flagged' must be provided.",
        )

    user_id = await current_user_id_or_none(request)
    async with db_pool.acquire() as conn:
        await assert_paper_ownership(conn, paper_id, user_id)
        try:
            await conn.execute(
                """INSERT INTO paper_user_state (paper_id, rating, flagged)
                VALUES ($1, $2, $3)
                ON CONFLICT (paper_id) DO UPDATE SET
                    rating = COALESCE($2, paper_user_state.rating),
                    flagged = COALESCE($3, paper_user_state.flagged)""",
                paper_id,
                rating,
                flagged,
            )
        except asyncpg.ForeignKeyViolationError as e:
            raise HTTPException(status_code=404, detail=f"Paper {paper_id} not found") from e

        # Fetch the current state to return accurate values
        row = await conn.fetchrow(
            "SELECT rating, flagged FROM paper_user_state WHERE paper_id = $1",
            paper_id,
        )

    return {
        "paper_id": paper_id,
        "rating": row["rating"] if row else rating,
        "flagged": row["flagged"] if row else flagged,
        "status": "updated",
    }
