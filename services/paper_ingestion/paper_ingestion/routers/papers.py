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
    ArchiveRequest,
    BulkActionRequest,
    DismissRequest,
    FeedbackRequest,
    FeedbackResponse,
    FeedCountsResponse,
    HardDeleteRequest,
    MarkReadResponse,
    PaperBriefResponse,
    PaperCreate,
    PaperDetailResponse,
    PaperResponse,
    PaperStatus,
    SaveRequest,
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
            conditions.append(
                f"((pus.status = ${len(params)} OR pus.paper_id IS NULL)"
                " AND NOT (COALESCE(pus.archived, FALSE) OR pus.status = 'archived'))"
            )
        elif status.value == "archived":
            joins.append("JOIN paper_user_state pus ON p.id = pus.paper_id")
            conditions.append("(COALESCE(pus.archived, FALSE) OR pus.status = 'archived')")
        elif status.value == "starred":
            joins.append("JOIN paper_user_state pus ON p.id = pus.paper_id")
            conditions.append("(COALESCE(pus.starred, FALSE) OR pus.status = 'starred')")
        else:
            joins.append("JOIN paper_user_state pus ON p.id = pus.paper_id")
            params.append(status.value)
            conditions.append(
                f"pus.status = ${len(params)}"
                " AND NOT (COALESCE(pus.archived, FALSE) OR pus.status = 'archived')"
            )

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
            """SELECT status,
                      (COALESCE(starred, FALSE) OR status = 'starred') AS starred,
                      (COALESCE(archived, FALSE) OR status = 'archived') AS archived,
                      COALESCE(preference, 'none') AS preference,
                      rating,
                      user_notes,
                      flagged
               FROM paper_user_state
               WHERE paper_id = $1 AND user_id IS NOT DISTINCT FROM $2
               LIMIT 1""",
            paper_id,
            user_id,
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
            starred=bool(user_state_row.get("starred")),
            archived=bool(user_state_row.get("archived")),
            preference=user_state_row.get("preference") or "none",
            rating=user_state_row["rating"],
            user_notes=user_state_row["user_notes"],
            flagged=bool(user_state_row["flagged"]),
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
            """INSERT INTO paper_user_state (paper_id, user_id, status)
               VALUES ($1, $2, 'read')
               ON CONFLICT (paper_id, user_id) DO UPDATE SET status = 'read'""",
            paper_id,
            user_id,
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

    When starring (starred=TRUE), also sets saved=TRUE so the paper enters the
    Library.  Unstarring does NOT unsave.

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
        current = await conn.fetchval(
            "SELECT COALESCE(starred, FALSE) FROM paper_user_state"
            " WHERE paper_id = $1 AND user_id IS NOT DISTINCT FROM $2",
            paper_id,
            user_id,
        )
        new_starred = not bool(current)
        await conn.execute(
            """INSERT INTO paper_user_state (paper_id, user_id, status, starred, saved)
               VALUES ($1, $2, 'new', $3, CASE WHEN $3 THEN TRUE ELSE FALSE END)
               ON CONFLICT (paper_id, user_id) DO UPDATE SET
                   starred = $3,
                   saved = CASE WHEN $3 THEN TRUE ELSE paper_user_state.saved END""",
            paper_id,
            user_id,
            new_starred,
        )
    return {"status": "ok", "paper_id": paper_id}


# ---------------------------------------------------------------------------
# PUT /api/papers/{paper_id}/archive
# ---------------------------------------------------------------------------


@router.put("/{paper_id}/archive", response_model=MarkReadResponse)
@limiter.limit("30/minute")
async def archive_paper(
    request: Request,
    paper_id: int,
    body: ArchiveRequest = ArchiveRequest(),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
):
    """Archive (or unarchive) a paper without changing its reading progress.

    Archiving requires the paper to already be saved (in the Library).
    Unarchiving has no precondition.
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
        if body.archive:
            saved_state = await conn.fetchval(
                "SELECT COALESCE(saved, FALSE) FROM paper_user_state"
                " WHERE paper_id = $1 AND user_id IS NOT DISTINCT FROM $2",
                paper_id,
                user_id,
            )
            if not saved_state:
                raise HTTPException(
                    status_code=409,
                    detail="Save before archiving — papers must be in Library first",
                )
        await conn.execute(
            """INSERT INTO paper_user_state (paper_id, user_id, status, archived)
               VALUES ($1, $2, 'new', $3)
               ON CONFLICT (paper_id, user_id) DO UPDATE SET archived = $3""",
            paper_id,
            user_id,
            body.archive,
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
    preference = feedback.preference
    if rating is None and flagged is None and preference is None:
        raise HTTPException(
            status_code=400,
            detail="At least one of 'rating', 'preference', or 'flagged' must be provided.",
        )

    user_id = await current_user_id_or_none(request)
    async with db_pool.acquire() as conn:
        await assert_paper_ownership(conn, paper_id, user_id)
        try:
            await conn.execute(
                """INSERT INTO paper_user_state (paper_id, user_id, rating, preference, flagged)
                VALUES ($1, $2, $3, COALESCE($4, 'none'), $5)
                ON CONFLICT (paper_id, user_id) DO UPDATE SET
                    rating = COALESCE($3, paper_user_state.rating),
                    preference = COALESCE($4, paper_user_state.preference),
                    flagged = COALESCE($5, paper_user_state.flagged)""",
                paper_id,
                user_id,
                rating,
                preference,
                flagged,
            )
        except asyncpg.ForeignKeyViolationError as e:
            raise HTTPException(status_code=404, detail=f"Paper {paper_id} not found") from e

        # Fetch the current state to return accurate values
        row = await conn.fetchrow(
            "SELECT rating, preference, flagged FROM paper_user_state"
            " WHERE paper_id = $1 AND user_id IS NOT DISTINCT FROM $2",
            paper_id,
            user_id,
        )

    return {
        "paper_id": paper_id,
        "rating": row["rating"] if row else rating,
        "preference": row["preference"] if row else preference,
        "flagged": row["flagged"] if row else flagged,
        "status": "updated",
    }


# ---------------------------------------------------------------------------
# GET /api/papers/feed/counts  — B1.4
# ---------------------------------------------------------------------------


@router.get("/feed/counts", response_model=FeedCountsResponse)
@limiter.limit("60/minute")
async def get_feed_counts(
    request: Request,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
):
    """Return per-bucket paper counts for the current user."""
    user_id = await current_user_id_or_none(request)
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                COALESCE(SUM(CASE WHEN COALESCE(pus.saved, FALSE) = FALSE
                                AND COALESCE(pus.dismissed, FALSE) = FALSE
                                THEN 1 ELSE 0 END), 0) AS inbox,
                COALESCE(SUM(CASE WHEN pus.saved = TRUE AND COALESCE(pus.dismissed, FALSE) = FALSE
                                AND COALESCE(pus.archived, FALSE) = FALSE
                                THEN 1 ELSE 0 END), 0) AS library,
                COALESCE(SUM(CASE WHEN pus.saved = TRUE AND pus.starred = TRUE
                                AND COALESCE(pus.dismissed, FALSE) = FALSE
                                THEN 1 ELSE 0 END), 0) AS starred,
                COALESCE(SUM(CASE WHEN pus.saved = TRUE AND pus.archived = TRUE
                                AND COALESCE(pus.dismissed, FALSE) = FALSE
                                THEN 1 ELSE 0 END), 0) AS archived,
                COALESCE(SUM(CASE WHEN pus.saved = TRUE AND pus.status = 'reading'
                                AND COALESCE(pus.dismissed, FALSE) = FALSE
                                THEN 1 ELSE 0 END), 0) AS reading,
                COALESCE(SUM(CASE WHEN pus.dismissed = TRUE THEN 1 ELSE 0 END), 0) AS trash,
                COALESCE(SUM(CASE WHEN COALESCE(pus.dismissed, FALSE) = FALSE
                                THEN 1 ELSE 0 END), 0) AS all_active
            FROM papers p
            LEFT JOIN paper_user_state pus ON pus.paper_id = p.id
              AND ($1::int IS NULL OR pus.user_id IS NOT DISTINCT FROM $1)
            WHERE p.user_id IS NOT DISTINCT FROM $1
            """,
            user_id,
        )
    return FeedCountsResponse(
        inbox=row["inbox"],
        library=row["library"],
        starred=row["starred"],
        archived=row["archived"],
        reading=row["reading"],
        trash=row["trash"],
        all_active=row["all_active"],
    )


# ---------------------------------------------------------------------------
# Lifecycle helpers — B1.1
# ---------------------------------------------------------------------------


async def _assert_paper_in_trash(
    conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,  # type: ignore[type-arg]
    paper_id: int,
    user_id: int | None,
) -> None:
    """Raise 409 if the paper has not been dismissed (not in Trash)."""
    dismissed = await conn.fetchval(
        "SELECT COALESCE(dismissed, FALSE) FROM paper_user_state"
        " WHERE paper_id = $1 AND user_id IS NOT DISTINCT FROM $2",
        paper_id,
        user_id,
    )
    if not dismissed:
        raise HTTPException(
            status_code=409,
            detail="Paper must be dismissed (in Trash) before hard-delete",
        )


async def _assert_confirm_title_matches(
    conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,  # type: ignore[type-arg]
    paper_id: int,
    confirm_title: str,
) -> None:
    """Raise 400 if ``confirm_title`` does not exactly match the paper's title."""
    title = await conn.fetchval(
        "SELECT title FROM papers WHERE id = $1",
        paper_id,
    )
    if title != confirm_title:
        raise HTTPException(
            status_code=400,
            detail="confirm_title does not match the paper's title",
        )


async def _upsert_user_state(
    conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,  # type: ignore[type-arg]
    paper_id: int,
    user_id: int | None,
    **fields: object,
) -> None:
    """COALESCE-aware upsert of arbitrary paper_user_state fields.

    Only the key/value pairs passed as ``**fields`` are set; existing columns
    not mentioned are preserved via ``DO UPDATE SET col = excluded.col``.
    """
    if not fields:
        return

    columns = list(fields.keys())
    values = list(fields.values())

    # Always include paper_id and user_id at positions $1/$2
    col_list = ", ".join(["paper_id", "user_id", "status"] + columns)
    placeholders = ", ".join(["$1", "$2", "'new'"] + [f"${i + 3}" for i in range(len(columns))])
    updates = ", ".join([f"{col} = ${i + 3}" for i, col in enumerate(columns)])

    sql = (
        f"INSERT INTO paper_user_state ({col_list}) "  # noqa: S608
        f"VALUES ({placeholders}) "
        f"ON CONFLICT (paper_id, user_id) DO UPDATE SET {updates}"
    )
    await conn.execute(sql, paper_id, user_id, *values)


async def _apply_bulk_action(
    conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,  # type: ignore[type-arg]
    paper_id: int,
    user_id: int | None,
    action: str,
) -> None:
    """Dispatch a single bulk action to the appropriate state mutation."""
    if action == "save":
        await _upsert_user_state(conn, paper_id, user_id, saved=True)
    elif action == "unsave":
        await _upsert_user_state(conn, paper_id, user_id, saved=False)
    elif action == "dismiss":
        await _upsert_user_state(conn, paper_id, user_id, dismissed=True, preference="down")
    elif action == "archive":
        await _upsert_user_state(conn, paper_id, user_id, archived=True)
    elif action == "unarchive":
        await _upsert_user_state(conn, paper_id, user_id, archived=False)
    elif action == "mark_read":
        await _upsert_user_state(conn, paper_id, user_id, status="read")
    elif action == "star":
        await _upsert_user_state(conn, paper_id, user_id, starred=True, saved=True)
    elif action == "unstar":
        await _upsert_user_state(conn, paper_id, user_id, starred=False)
    else:
        raise ValueError(f"Unknown bulk action: {action}")


# ---------------------------------------------------------------------------
# PUT /api/papers/{paper_id}/save  — B1.1
# ---------------------------------------------------------------------------


@router.put("/{paper_id}/save", response_model=MarkReadResponse)
@limiter.limit("30/minute")
async def save_paper(
    request: Request,
    paper_id: int,
    body: SaveRequest,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
):
    """Save a paper to the Library (optionally also star it)."""
    user_id = await current_user_id_or_none(request)
    async with db_pool.acquire() as conn:
        await assert_paper_ownership(conn, paper_id, user_id)
        row = await conn.fetchrow("SELECT id FROM papers WHERE id = $1", paper_id)
        if not row:
            raise HTTPException(status_code=404, detail="Paper not found")
        extra: dict[str, object] = {"saved": True}
        if body.star:
            extra["starred"] = True
        await _upsert_user_state(conn, paper_id, user_id, **extra)
    return {"status": "ok", "paper_id": paper_id}


# ---------------------------------------------------------------------------
# PUT /api/papers/{paper_id}/unsave  — B1.1
# ---------------------------------------------------------------------------


@router.put("/{paper_id}/unsave", response_model=MarkReadResponse)
@limiter.limit("30/minute")
async def unsave_paper(
    request: Request,
    paper_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
):
    """Remove a paper from the Library (preserves star/archive flags)."""
    user_id = await current_user_id_or_none(request)
    async with db_pool.acquire() as conn:
        await assert_paper_ownership(conn, paper_id, user_id)
        row = await conn.fetchrow("SELECT id FROM papers WHERE id = $1", paper_id)
        if not row:
            raise HTTPException(status_code=404, detail="Paper not found")
        await _upsert_user_state(conn, paper_id, user_id, saved=False)
    return {"status": "ok", "paper_id": paper_id}


# ---------------------------------------------------------------------------
# PUT /api/papers/{paper_id}/dismiss  — B1.1
# ---------------------------------------------------------------------------


@router.put("/{paper_id}/dismiss", response_model=MarkReadResponse)
@limiter.limit("30/minute")
async def dismiss_paper(
    request: Request,
    paper_id: int,
    body: DismissRequest,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
):
    """Dismiss a paper to Trash (sets dismissed=TRUE, preference='down')."""
    user_id = await current_user_id_or_none(request)
    async with db_pool.acquire() as conn:
        await assert_paper_ownership(conn, paper_id, user_id)
        row = await conn.fetchrow("SELECT id FROM papers WHERE id = $1", paper_id)
        if not row:
            raise HTTPException(status_code=404, detail="Paper not found")
        if body.also_zotero:
            logger.info(
                "Zotero remove requested for paper %d but handler not yet implemented",
                paper_id,
            )
        await _upsert_user_state(conn, paper_id, user_id, dismissed=True, preference="down")
    return {"status": "ok", "paper_id": paper_id}


# ---------------------------------------------------------------------------
# PUT /api/papers/{paper_id}/restore  — B1.1
# ---------------------------------------------------------------------------


@router.put("/{paper_id}/restore", response_model=MarkReadResponse)
@limiter.limit("30/minute")
async def restore_paper(
    request: Request,
    paper_id: int,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
):
    """Restore a dismissed paper from Trash (sets dismissed=FALSE, preference='none')."""
    user_id = await current_user_id_or_none(request)
    async with db_pool.acquire() as conn:
        await assert_paper_ownership(conn, paper_id, user_id)
        row = await conn.fetchrow("SELECT id FROM papers WHERE id = $1", paper_id)
        if not row:
            raise HTTPException(status_code=404, detail="Paper not found")
        await _upsert_user_state(conn, paper_id, user_id, dismissed=False, preference="none")
    return {"status": "ok", "paper_id": paper_id}


# ---------------------------------------------------------------------------
# DELETE /api/papers/{paper_id}  — B1.1 hard delete
# ---------------------------------------------------------------------------


@router.delete("/{paper_id}")
@limiter.limit("10/minute")
async def hard_delete_paper(
    request: Request,
    paper_id: int,
    body: HardDeleteRequest,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
):
    """Permanently delete a paper (must be in Trash; title confirmation required)."""
    user_id = await current_user_id_or_none(request)
    async with db_pool.acquire() as conn:
        await assert_paper_ownership(conn, paper_id, user_id)
        await _assert_paper_in_trash(conn, paper_id, user_id)
        await _assert_confirm_title_matches(conn, paper_id, body.confirm_title)
        async with conn.transaction():
            if body.also_zotero:
                logger.info(
                    "Zotero remove requested for paper %d but handler not yet implemented",
                    paper_id,
                )
            # Deferred import: delete_paper_vectors is wired by B1.8 in parallel.
            # At runtime this will fail gracefully if B1.8 hasn't landed yet;
            # tests can mock 'paper_ingestion.ingestion.embedder.delete_paper_vectors'.
            from paper_ingestion.ingestion.embedder import delete_paper_vectors  # noqa: PLC0415

            await delete_paper_vectors(paper_id)
            await conn.execute("DELETE FROM papers WHERE id = $1", paper_id)
    return {"deleted": paper_id}


# ---------------------------------------------------------------------------
# POST /api/papers/bulk  — B1.1 bulk action
# ---------------------------------------------------------------------------


@router.post("/bulk")
@limiter.limit("10/minute")
async def bulk_action_papers(
    request: Request,
    body: BulkActionRequest,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
):
    """Apply a lifecycle action to multiple papers atomically.

    Returns ``{"succeeded": [...], "failed": [{"paper_id": int, "error": str}]}``.
    Partial failures are collected; the outer transaction is committed even when
    individual papers fail.
    """
    user_id = await current_user_id_or_none(request)
    succeeded: list[int] = []
    failed: list[dict[str, object]] = []

    async with db_pool.acquire() as conn:
        async with conn.transaction():
            for paper_id in body.paper_ids:
                try:
                    await assert_paper_ownership(conn, paper_id, user_id)
                    await _apply_bulk_action(conn, paper_id, user_id, body.action)
                    succeeded.append(paper_id)
                except Exception as exc:  # noqa: BLE001
                    failed.append({"paper_id": paper_id, "error": str(exc)})

    return {"succeeded": succeeded, "failed": failed}
