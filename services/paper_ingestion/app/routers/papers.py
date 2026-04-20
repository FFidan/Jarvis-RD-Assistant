"""Paper CRUD and metadata endpoints."""

import logging
from typing import Annotated

import asyncpg
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from jarvis_common import escape_like

from app.converters import (
    row_to_chunk_response,
    row_to_paper_response,
    row_to_summary_response,
)
from app.deps import get_db_pool, limiter
from app.models import (
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
from app.services.pdf_workflow import upsert_paper

logger = logging.getLogger(__name__)
router = APIRouter(tags=["papers"])


# ---------------------------------------------------------------------------
# GET /api/papers/brief
# ---------------------------------------------------------------------------


@router.get("/api/papers/brief", response_model=list[PaperBriefResponse])
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


@router.get("/api/papers", response_model=list[PaperResponse])
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
    from app.converters import hybrid_dict_to_paper_response
    from app.embedder import Embedder

    # ------------------------------------------------------------------
    # Hybrid search path: q is set and no other filters are active
    # ------------------------------------------------------------------
    has_extra_filters = any([status, source_type, topic_id])
    if q and not has_extra_filters:
        embedder: Embedder | None = getattr(request.app.state, "embedder", None)
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
    param_idx = 1

    if topic_id is not None:
        joins.append("JOIN paper_topics pt ON p.id = pt.paper_id")
        conditions.append(f"pt.topic_id = ${param_idx}")
        params.append(topic_id)
        param_idx += 1

    if status is not None:
        if status.value == "new":
            # Papers without a user_state row are implicitly "new"
            joins.append("LEFT JOIN paper_user_state pus ON p.id = pus.paper_id")
            conditions.append(f"(pus.status = ${param_idx} OR pus.paper_id IS NULL)")
        else:
            joins.append("JOIN paper_user_state pus ON p.id = pus.paper_id")
            conditions.append(f"pus.status = ${param_idx}")
        params.append(status.value)
        param_idx += 1

    if source_type is not None:
        conditions.append(f"p.source_type = ${param_idx}")
        params.append(source_type.value)
        param_idx += 1

    if q:
        conditions.append(f"p.search_vector @@ plainto_tsquery('english', ${param_idx})")
        params.append(q)
        param_idx += 1

    if joins:
        query += " " + " ".join(joins)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)

    query += f" ORDER BY p.created_at DESC LIMIT ${param_idx} OFFSET ${param_idx + 1}"
    params.extend([limit, offset])

    async with db_pool.acquire() as conn:
        rows = await conn.fetch(query, *params)

    return [row_to_paper_response(row) for row in rows]


# ---------------------------------------------------------------------------
# GET /api/papers/{paper_id}
# ---------------------------------------------------------------------------


@router.get("/api/papers/{paper_id}", response_model=PaperDetailResponse)
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
    async with db_pool.acquire() as conn:
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

    return PaperDetailResponse(paper=paper, summary=summary, chunks=chunks, user_state=user_state)


# ---------------------------------------------------------------------------
# PUT /api/papers/{paper_id}/read
# ---------------------------------------------------------------------------


@router.put("/api/papers/{paper_id}/read", response_model=MarkReadResponse)
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
    async with db_pool.acquire() as conn:
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
# POST /api/papers/batch-save
# ---------------------------------------------------------------------------


@router.post("/api/papers/batch-save", response_model=list[PaperResponse])
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


@router.post("/api/papers/{paper_id}/feedback", response_model=FeedbackResponse)
@limiter.limit("30/minute")
async def submit_feedback(
    request: Request,
    paper_id: int,
    rating: int | None = Query(default=None, ge=1, le=5),
    flagged: bool | None = Query(default=None),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
):
    """Allow users to rate a paper (1-5) and/or flag suspicious summaries."""
    if rating is None and flagged is None:
        raise HTTPException(
            status_code=400,
            detail="At least one of 'rating' or 'flagged' must be provided.",
        )

    async with db_pool.acquire() as conn:
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
        except asyncpg.ForeignKeyViolationError:
            raise HTTPException(status_code=404, detail=f"Paper {paper_id} not found")

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
