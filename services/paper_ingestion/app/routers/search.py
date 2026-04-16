"""Search, feed, discovery, and relevance scoring endpoints."""

import logging
from datetime import date
from typing import NoReturn

import asyncpg
import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.converters import (
    deduplicate_by_paper_id,
    row_to_feed_paper,
    row_to_paper_response,
)
from app.deps import get_db_pool, get_embedder, get_http_client, limiter
from app.embedder import Embedder
from app.models import (
    DiscoverRequest,
    DiscoveryResultItem,
    FeedResponse,
    HybridSearchResult,
    PaperCreate,
    PaperResponse,
    RelevanceScoreResponse,
    SearchRequest,
    SimilarPaperResult,
    priority_level,
)
from app.services.feed_query import (
    build_feed_queries,
    derive_feed_search_mode,
    fetch_feed_rows,
)
from app.services.pdf_workflow import upsert_paper
from app.services.source_helper import get_source_for_type

logger = logging.getLogger(__name__)
router = APIRouter(tags=["search"])


def _raise_source_search_error(
    source_type: str, exc: httpx.HTTPStatusError, *, api_key_configured: bool
) -> NoReturn:
    """Translate source API failures into stable user-facing HTTP errors."""
    status_code = exc.response.status_code
    if source_type == "semantic_scholar" and status_code == 429:
        detail = "Semantic Scholar rate limit reached. Retry later"
        if not api_key_configured:
            detail += " or configure an API key in Settings > Sources."
        raise HTTPException(status_code=429, detail=detail) from exc
    raise HTTPException(status_code=502, detail=f"Source API error: {status_code}") from exc


# ---------------------------------------------------------------------------
# POST /api/search
# ---------------------------------------------------------------------------


@router.post("/api/search", response_model=list[PaperResponse])
@limiter.limit("30/minute")
async def search_papers(
    request: Request,
    body: SearchRequest,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    http_client: httpx.AsyncClient = Depends(get_http_client),
) -> list[PaperResponse]:
    """Search for papers and upsert results into the database.

    Parameters
    ----------
    body : SearchRequest
        Query string, source type, and max_results.

    Returns
    -------
    list[PaperResponse]
        Papers found (metadata from source API, never LLM-generated).
    """
    source = await get_source_for_type(body.source, db_pool, http_client, request=request)

    try:
        papers = await source.search(body.query, body.max_results)
    except httpx.HTTPStatusError as e:
        _raise_source_search_error(
            source.source_type,
            e,
            api_key_configured=bool(source.config.config.get("api_key")),
        )

    results: list[PaperResponse] = []
    async with db_pool.acquire() as conn:
        for paper in papers:
            row = await upsert_paper(conn, paper)
            results.append(row_to_paper_response(row))

    return results


@router.post("/api/search-preview", response_model=list[PaperCreate])
@limiter.limit("30/minute")
async def search_papers_preview(
    request: Request,
    body: SearchRequest,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    http_client: httpx.AsyncClient = Depends(get_http_client),
) -> list[PaperCreate]:
    """Search papers without saving to DB -- for preview & select flow."""
    source = await get_source_for_type(body.source, db_pool, http_client, request=request)
    try:
        return await source.search(
            body.query,
            body.max_results,
            year_from=body.year_from,
            year_to=body.year_to,
            sort_by=body.sort_by,
            author=body.author,
        )
    except httpx.HTTPStatusError as e:
        _raise_source_search_error(
            source.source_type,
            e,
            api_key_configured=bool(source.config.config.get("api_key")),
        )


# ---------------------------------------------------------------------------
# POST /api/papers/search-hybrid
# ---------------------------------------------------------------------------


@router.post("/api/papers/search-hybrid", response_model=list[HybridSearchResult])
@limiter.limit("30/minute")
async def search_hybrid(
    request: Request,
    body: SearchRequest,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    embedder: Embedder | None = Depends(get_embedder),
) -> list[dict]:
    """Search papers using hybrid BM25 + semantic search with RRF fusion.

    Combines PostgreSQL full-text search (BM25) and Qdrant semantic
    similarity via Reciprocal Rank Fusion.  Returns results ranked by
    fused relevance score.

    Parameters
    ----------
    body : SearchRequest
        ``query`` (str) and ``max_results`` (int, default 10).

    Returns
    -------
    list[dict]
        Papers with ``id``, ``title``, ``authors``, ``url``, ``abstract``,
        ``published_date``, ``rrf_score``, ``bm25_rank``, ``semantic_rank``.
    """
    if embedder is None or embedder.qdrant is None:
        raise HTTPException(
            status_code=503,
            detail="Embedder or Qdrant unavailable for hybrid search",
        )

    results = await embedder.hybrid_search(body.query, db_pool, limit=body.max_results)
    return results


# ---------------------------------------------------------------------------
# GET /api/papers/feed
# ---------------------------------------------------------------------------


@router.get("/api/papers/feed", response_model=FeedResponse)
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
    q: str | None = Query(default=None),
    statuses: str | None = Query(default=None),
    source_types: str | None = Query(default=None),
    topic_names: str | None = Query(default=None),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    recommended: bool = False,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
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
    source_types : str, optional
        Comma-separated list of source types (e.g. ``arxiv,semantic_scholar``).
    topic_names : str, optional
        Comma-separated list of topic names.
    date_from, date_to : date, optional
        Created-at date range boundaries.
    db_pool : asyncpg.Pool
        Injected database pool.

    Returns
    -------
    FeedResponse
        ``{papers: [...], total: N}``
    """
    query_parts = build_feed_queries(
        unread_only=unread_only,
        sort=sort,
        limit=limit,
        offset=offset,
        q=q,
        statuses=statuses,
        source_types=source_types,
        topic_names=topic_names,
        date_from=date_from,
        date_to=date_to,
        recommended=recommended,
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


# ---------------------------------------------------------------------------
# GET /api/similar/{paper_id}
# ---------------------------------------------------------------------------


@router.get("/api/similar/{paper_id}", response_model=list[SimilarPaperResult])
@limiter.limit("20/minute")
async def find_similar_papers(
    request: Request,
    paper_id: int,
    limit: int = Query(default=5, ge=1, le=20),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    embedder: Embedder | None = Depends(get_embedder),
) -> list[dict]:
    """Find papers semantically similar to the given paper.

    Uses Qdrant vector similarity search on paper chunk embeddings.

    Parameters
    ----------
    paper_id : int
        Database paper ID to find similar papers for.
    limit : int
        Maximum number of similar papers to return (1-20, default 5).

    Returns
    -------
    list[dict]
        Similar papers with similarity scores and matching snippets.
    """
    async with db_pool.acquire() as conn:
        paper_row = await conn.fetchrow(
            "SELECT id, title, abstract FROM papers WHERE id = $1", paper_id
        )
        if not paper_row:
            raise HTTPException(status_code=404, detail="Paper not found")

        title = paper_row["title"]
        abstract = paper_row["abstract"] or ""
        query_text = f"{title}. {abstract}"

        if embedder is None or embedder.qdrant is None:
            raise HTTPException(status_code=503, detail="Search service unavailable")
        results = await embedder.search_similar(
            query_text=query_text,
            limit=limit * 3,  # extra results for dedup
            paper_id_filter=paper_id,
            score_threshold=0.6,
        )

        # Deduplicate by paper_id, keep highest score per paper
        deduped = deduplicate_by_paper_id(results)

        # Sort by score descending
        sorted_results = sorted(deduped, key=lambda x: x["score"], reverse=True)
        sorted_results = sorted_results[:limit]

        # Enrich with paper metadata (batch query to avoid N+1)
        paper_ids = [r["paper_id"] for r in sorted_results]
        if paper_ids:
            meta_rows = await conn.fetch(
                "SELECT id, title, authors, url FROM papers WHERE id = ANY($1::int[])",
                paper_ids,
            )
            meta_map = {row["id"]: row for row in meta_rows}
        else:
            meta_map = {}

        enriched: list[dict] = []
        for r in sorted_results:
            meta = meta_map.get(r["paper_id"])
            if meta:
                enriched.append(
                    {
                        "paper_id": r["paper_id"],
                        "title": meta["title"],
                        "authors": meta["authors"],
                        "url": meta["url"],
                        "similarity_score": round(r["score"], 3),
                        "matching_snippet": r.get("content", ""),
                    }
                )

    return enriched


# ---------------------------------------------------------------------------
# POST /api/discover (seed-based discovery)
# ---------------------------------------------------------------------------


@router.post("/api/discover", response_model=list[DiscoveryResultItem])
@limiter.limit("10/minute")
async def discover_papers(
    request: Request,
    body: "DiscoverRequest",
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    embedder: Embedder | None = Depends(get_embedder),
) -> list[dict]:
    """Discover papers similar to a set of seed papers.

    Uses Qdrant's RecommendQuery with AVERAGE_VECTOR strategy to find
    papers that are semantically similar to the provided seeds.

    Parameters
    ----------
    body : DiscoverRequest
        Request with seed paper_ids, limit, and score_threshold.

    Returns
    -------
    list[dict]
        Discovered papers with metadata and similarity scores.
    """
    # Validate that all seed paper IDs exist
    async with db_pool.acquire() as conn:
        existing = await conn.fetch(
            "SELECT id FROM papers WHERE id = ANY($1::int[])", body.paper_ids
        )
        existing_ids = {row["id"] for row in existing}
        missing = [pid for pid in body.paper_ids if pid not in existing_ids]
        if missing:
            raise HTTPException(
                status_code=404,
                detail=f"Papers not found: {missing}",
            )

    if embedder is None or embedder.qdrant is None:
        raise HTTPException(status_code=503, detail="Search service unavailable")
    results = await embedder.discover_from_seeds(
        seed_paper_ids=body.paper_ids,
        db_pool=db_pool,
        limit=body.limit,
        score_threshold=body.score_threshold,
    )

    if not results:
        return []

    # Enrich with paper metadata
    paper_ids = [r["paper_id"] for r in results]
    async with db_pool.acquire() as conn:
        meta_rows = await conn.fetch(
            "SELECT id, title, authors, url FROM papers WHERE id = ANY($1::int[])",
            paper_ids,
        )
    meta_map = {row["id"]: row for row in meta_rows}

    enriched: list[dict] = []
    for r in results:
        meta = meta_map.get(r["paper_id"])
        if meta:
            enriched.append(
                {
                    "paper_id": r["paper_id"],
                    "title": meta["title"],
                    "authors": meta["authors"],
                    "url": meta["url"],
                    "similarity_score": round(r["score"], 3),
                    "matching_snippet": r.get("content", ""),
                }
            )

    return enriched


# ---------------------------------------------------------------------------
# POST /api/relevance-score
# ---------------------------------------------------------------------------


@router.post("/api/relevance-score", response_model=RelevanceScoreResponse)
@limiter.limit("30/minute")
async def compute_relevance(
    request: Request,
    paper_id: int = Query(...),
    topic_id: int = Query(...),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    embedder: Embedder | None = Depends(get_embedder),
):
    """Compute and store relevance score between a paper and a topic."""
    async with db_pool.acquire() as conn:
        # Fetch paper and topic data in one round-trip
        paper = await conn.fetchrow("SELECT title, abstract FROM papers WHERE id = $1", paper_id)
        topic = await conn.fetchrow("SELECT query_terms FROM topics WHERE id = $1", topic_id)
        if not paper:
            raise HTTPException(404, f"Paper {paper_id} not found")
        if not topic:
            raise HTTPException(404, f"Topic {topic_id} not found")

        paper_text = f"{paper['title']}. {paper['abstract'] or ''}"
        if embedder is None or embedder.qdrant is None:
            raise HTTPException(status_code=503, detail="Search service unavailable")
        score = await embedder.compute_relevance(paper_text, topic["query_terms"])

        try:
            await conn.execute(
                """INSERT INTO paper_topics (paper_id, topic_id, relevance_score)
                VALUES ($1, $2, $3)
                ON CONFLICT (paper_id, topic_id) DO UPDATE SET relevance_score = $3""",
                paper_id,
                topic_id,
                score,
            )
        except asyncpg.ForeignKeyViolationError:
            raise HTTPException(404, "Referenced paper or topic not found")

    return {"paper_id": paper_id, "topic_id": topic_id, "relevance_score": score}
