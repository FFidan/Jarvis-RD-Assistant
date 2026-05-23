"""Feed and search endpoints: list_papers_brief, list_papers, get_feed_counts."""

import logging

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from jarvis_common import ErrorResponse, escape_like
from jarvis_common.auth import get_current_user_id

from paper_ingestion import papers_service
from paper_ingestion.converters import row_to_paper_response
from paper_ingestion.deps import get_db_pool, get_optional_embedder, limiter
from paper_ingestion.models import (
    FeedCountsResponse,
    PaperBriefResponse,
    PaperResponse,
    SourceType,
)
from paper_ingestion.queries.predicates import VIEW_PREDICATES

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
    caller_id: int = Depends(get_current_user_id),
) -> list[dict]:
    """Return lightweight paper list for selector dropdowns."""
    async with db_pool.acquire() as conn:
        if search:
            rows = await conn.fetch(
                """SELECT p.id, p.title, p.source_type, p.published_date
                   FROM papers p
                   JOIN user_library ul ON ul.paper_id = p.id AND ul.user_id = $2
                   WHERE p.title ILIKE '%' || $1 || '%' ESCAPE '\\'
                   ORDER BY p.created_at DESC
                   LIMIT 200""",
                escape_like(search),
                caller_id,
            )
        else:
            rows = await conn.fetch(
                """SELECT p.id, p.title, p.source_type, p.published_date
                   FROM papers p
                   JOIN user_library ul ON ul.paper_id = p.id AND ul.user_id = $1
                   ORDER BY p.created_at DESC
                   LIMIT 200""",
                caller_id,
            )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# GET /api/papers
# ---------------------------------------------------------------------------


@router.get("", response_model=list[PaperResponse])
@limiter.limit("60/minute")
async def list_papers(
    request: Request,
    view: str | None = Query(default=None, max_length=64),
    source_type: SourceType | None = None,
    topic_id: int | None = None,
    q: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    embedder=Depends(get_optional_embedder),
    user_id: int = Depends(get_current_user_id),
) -> list[PaperResponse]:
    """List papers with optional filters.

    When a search query ``q`` is provided, attempts hybrid BM25 + semantic
    search via Reciprocal Rank Fusion.  Falls back to BM25-only if the
    embedder or Qdrant is unavailable.
    """
    from paper_ingestion.converters import hybrid_dict_to_paper_response  # noqa: PLC0415

    if view is not None and view not in VIEW_PREDICATES:
        raise HTTPException(
            status_code=422,
            detail=(f"Unknown view '{view}'. Valid views: {sorted(VIEW_PREDICATES.keys())}"),
        )

    # ------------------------------------------------------------------
    # Hybrid search path: q is set and no other filters are active
    # ------------------------------------------------------------------
    has_extra_filters = any([view, source_type, topic_id])
    if q and not has_extra_filters:
        if embedder is not None and embedder.qdrant is not None:
            try:
                hybrid_results = await embedder.hybrid_search(
                    q, db_pool, limit=limit, offset=offset, user_id=user_id
                )
                return [await hybrid_dict_to_paper_response(r, db_pool) for r in hybrid_results]
            except Exception:
                logger.warning(
                    "Hybrid search failed, falling back to BM25-only",
                    exc_info=True,
                )

    # ------------------------------------------------------------------
    # Standard / fallback BM25 query path
    # W1-D1-008: use websearch_to_tsquery consistently (same as hybrid path)
    # ------------------------------------------------------------------
    query = "SELECT p.* FROM papers p"
    joins: list[str] = []
    conditions: list[str] = []
    params: list = []

    if topic_id is not None:
        joins.append("JOIN paper_topics pt ON p.id = pt.paper_id")
        params.append(topic_id)
        conditions.append(f"pt.topic_id = ${len(params)}")

    if user_id is not None:
        params.append(user_id)
        joins.append(f"JOIN user_library ul ON ul.paper_id = p.id AND ul.user_id = ${len(params)}")

    if view is not None:
        params.append(user_id)
        joins.append(
            "LEFT JOIN paper_user_state pus ON pus.paper_id = p.id"
            f" AND pus.user_id = ${len(params)}"
        )
        conditions.append(VIEW_PREDICATES[view])

    if source_type is not None:
        params.append(source_type.value)
        conditions.append(f"p.source_type = ${len(params)}")

    if q:
        # W1-D1-008: websearch_to_tsquery (consistent with hybrid BM25 leg)
        params.append(q)
        conditions.append(f"p.search_vector @@ websearch_to_tsquery('english', ${len(params)})")

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
# GET /api/papers/feed/counts  — 10 named views per spec §6
# ---------------------------------------------------------------------------


@router.get("/feed/counts", response_model=FeedCountsResponse)
@limiter.limit("60/minute")
async def get_feed_counts(
    request: Request,
    scope: str = Query(default="library", max_length=16),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(get_current_user_id),
):
    """Return per-bucket paper counts (C3: delegates to papers_service)."""
    _ = request  # required by @limiter.limit; not used in body
    return await papers_service.get_feed_counts(scope, db_pool, user_id)
