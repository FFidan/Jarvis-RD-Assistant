"""Feed and search endpoints: list_papers_brief, list_papers, get_feed_counts."""

import logging
from datetime import date

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from jarvis_common import ErrorResponse, escape_like
from jarvis_common.auth import get_current_user_id

from paper_ingestion import papers_service
from paper_ingestion.converters import row_to_feed_paper, row_to_paper_response
from paper_ingestion.deps import get_db_pool, get_optional_embedder, limiter
from paper_ingestion.models import (
    FeedCountsResponse,
    FeedResponse,
    PaperBriefResponse,
    PaperResponse,
    SourceType,
    priority_level,
)
from paper_ingestion.queries.predicates import VIEW_PREDICATES
from paper_ingestion.services.feed_query import (
    build_feed_queries,
    derive_feed_search_mode,
    fetch_feed_rows,
)

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
    q: str | None = Query(default=None, max_length=500),
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
    from paper_ingestion.converters import batch_hybrid_results_to_paper_responses  # noqa: PLC0415

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
                return await batch_hybrid_results_to_paper_responses(hybrid_results, db_pool)
            except Exception:
                logger.warning(
                    "Hybrid search failed, falling back to BM25-only",
                    exc_info=True,
                )

    # ------------------------------------------------------------------
    # Standard / fallback BM25 query path
    # Use websearch_to_tsquery consistently (same as hybrid path)
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
        # websearch_to_tsquery (consistent with hybrid BM25 leg)
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
# GET /api/papers/feed/counts  — 10 named views
# ---------------------------------------------------------------------------


@router.get("/feed/counts", response_model=FeedCountsResponse)
@limiter.limit("60/minute")
async def get_feed_counts(
    request: Request,
    scope: str = Query(default="library", max_length=16),
    view: str | None = Query(default=None, max_length=64),
    source: SourceType | None = None,
    topic_id: int | None = None,
    untagged: bool = False,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(get_current_user_id),
) -> FeedCountsResponse:
    """Return per-bucket paper counts (C3: delegates to papers_service)."""
    _ = request  # required by @limiter.limit; not used in body
    return await papers_service.get_feed_counts(
        scope,
        db_pool,
        user_id,
        view=view,
        source=source.value if source is not None else None,
        topic_id=topic_id,
        untagged=untagged,
    )


# ---------------------------------------------------------------------------
# GET /api/papers/feed  — paginated What's-New view (consolidated from feed.py)
# ---------------------------------------------------------------------------


@router.get("/feed", response_model=FeedResponse)
@limiter.limit("60/minute")
async def list_feed_papers(
    request: Request,
    unread_only: bool = False,
    sort: str = Query(
        default="discovered_at",
        pattern="^(discovered_at|priority|published_date|title|citation_count|recommendation)$",
    ),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    q: str | None = Query(default=None, max_length=500),
    statuses: str | None = Query(default=None, max_length=500),
    source_types: str | None = Query(default=None, max_length=500),
    topic_names: str | None = Query(default=None, max_length=500),
    topic_id: int | None = Query(default=None),
    untagged: bool = Query(default=False),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    recommended: bool = False,
    include_zotero_notes: bool = Query(default=False),
    view: str | None = Query(default=None, max_length=64),
    scope: str = Query(default="library", max_length=16),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    user_id: int = Depends(get_current_user_id),
) -> FeedResponse:
    """Return papers for the What's New feed.

    Parameters
    ----------
    request : Request
        FastAPI request (needed by rate limiter).
    unread_only : bool
        When True, return only unread papers.
    sort : str
        Sort order: ``discovered_at`` (newest first) or ``priority``
        (highest priority_score first).
    limit, offset : int
        Pagination parameters.
    q : str, optional
        Full-text search query.
    statuses : str, optional
        Comma-separated list of user statuses to filter by (e.g. ``new,reading``).
        Deprecated — use ``view`` instead.
    view : str, optional
        Named view predicate: one of ``inbox``, ``library``, ``reading_list``,
        ``reading``, ``done``, ``starred``, ``trash``, ``active``, ``kept``,
        ``all_non_trash``.  Takes precedence over ``statuses`` when both are
        supplied.
    source_types : str, optional
        Comma-separated list of source types (e.g. ``arxiv,semantic_scholar``).
    topic_names : str, optional
        Comma-separated list of topic names.
    topic_id : int, optional
        Restrict to papers tagged with this topic id (via ``paper_topics``).
    untagged : bool
        When True, restrict to papers with no ``paper_topics`` row.
    date_from, date_to : date, optional
        Created-at date range boundaries.
    include_zotero_notes : bool
        Include Zotero-imported note/highlight full-text matches when ``q`` is set.
    db_pool : asyncpg.Pool
        Injected database pool.

    Returns
    -------
    FeedResponse
        ``{papers: [...], total: N}``
    """
    if view is not None and view not in VIEW_PREDICATES:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown view {view!r}. Valid values: {sorted(VIEW_PREDICATES)}",
        )
    if scope not in {"library", "corpus"}:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown scope {scope!r}. Valid values: ['corpus', 'library']",
        )
    query_parts = build_feed_queries(
        unread_only=unread_only,
        sort=sort,
        limit=limit,
        offset=offset,
        q=q,
        statuses=statuses,
        source_types=source_types,
        topic_names=topic_names,
        topic_id=topic_id,
        untagged=untagged,
        date_from=date_from,
        date_to=date_to,
        recommended=recommended,
        include_zotero_notes=include_zotero_notes,
        user_id=user_id,
        view=view,
        scope=scope,
    )

    async with db_pool.acquire() as conn:
        rows = await fetch_feed_rows(conn, query_parts)
        count_row = await conn.fetchval(query_parts.count_query, *query_parts.count_params)

    papers = [row_to_feed_paper(row) for row in rows]
    for paper in papers:
        paper.priority_level = priority_level(paper.priority_score)

    return FeedResponse(
        papers=papers,
        total=count_row or 0,
        search_mode=derive_feed_search_mode(q),
    )
