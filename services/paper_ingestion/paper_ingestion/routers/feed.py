"""Research feed endpoint — paginated What's-New view over the local library.

Extracted from ``routers/search.py`` (GOD-001):

* ``GET /api/papers/feed`` — paginated, filterable feed of stored papers.
"""

from datetime import date

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from jarvis_common.auth import get_current_user_id

from paper_ingestion.converters import row_to_feed_paper
from paper_ingestion.deps import get_db_pool, limiter
from paper_ingestion.models import FeedResponse, priority_level
from paper_ingestion.queries.predicates import VIEW_PREDICATES
from paper_ingestion.services.feed_query import (
    build_feed_queries,
    derive_feed_search_mode,
    fetch_feed_rows,
)

router = APIRouter(prefix="/api", tags=["feed"])


# ---------------------------------------------------------------------------
# GET /api/papers/feed
# ---------------------------------------------------------------------------


@router.get("/papers/feed", response_model=FeedResponse)
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

    # Note: pool.acquire() without an explicit transaction uses auto-commit mode.
    # In asyncpg, a failed statement in auto-commit does NOT leave the connection
    # in an error state, so `conn` can safely be reused after the except clause.
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
