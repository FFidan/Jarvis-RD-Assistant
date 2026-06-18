"""Discovery endpoints — seed-based recommendations and paper-similarity lookup.

Extracted from ``routers/search.py`` (GOD-001):

* ``POST /api/discover``        — seed-based discovery via Qdrant RecommendQuery
* ``GET  /api/similar/{paper_id}`` — find papers semantically similar to one paper
"""

import asyncpg
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from jarvis_common.auth import current_user_id_strict
from jarvis_common.db_helpers import assert_paper_ownership

from paper_ingestion.converters import deduplicate_by_paper_id
from paper_ingestion.deps import get_db_pool, get_embedder, limiter
from paper_ingestion.ingestion.embedder import Embedder
from paper_ingestion.models import (
    DiscoverRequest,
    DiscoveryResultItem,
    SimilarPaperResult,
)
from paper_ingestion.rag.exceptions import QdrantUnavailableError

router = APIRouter(prefix="/api", tags=["discovery"])


# ---------------------------------------------------------------------------
# GET /api/similar/{paper_id}
# ---------------------------------------------------------------------------


@router.get("/similar/{paper_id}", response_model=list[SimilarPaperResult])
@limiter.limit("20/minute")
async def find_similar_papers(
    request: Request,
    paper_id: int,
    limit: int = Query(default=5, ge=1, le=20),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    embedder: Embedder | None = Depends(get_embedder),
    user_id: int = Depends(current_user_id_strict),
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
        await assert_paper_ownership(conn, paper_id, user_id)
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
        try:
            results = await embedder.search_similar(
                query_text=query_text,
                limit=limit * 3,  # extra results for dedup
                paper_id_filter=paper_id,
                score_threshold=0.6,
                user_id=user_id,
            )
        except QdrantUnavailableError as exc:
            raise HTTPException(status_code=503, detail="Search service unavailable") from exc

        # Deduplicate by paper_id, keep highest score per paper
        deduped = deduplicate_by_paper_id(results)

        # Sort by score descending
        sorted_results = sorted(deduped, key=lambda x: x["score"], reverse=True)
        sorted_results = sorted_results[:limit]

        # Enrich with paper metadata (batch query to avoid N+1)
        paper_ids = [r["paper_id"] for r in sorted_results]
        if paper_ids:
            # Papers are global; no per-paper owner filter remains. The
            # seed-paper ownership check above already enforces "you may
            # only discover from your own seeds".
            meta_rows = await conn.fetch(
                """SELECT id, title, authors, url FROM papers
                   WHERE id = ANY($1::int[])""",
                paper_ids,
            )
            _ = user_id  # retained for future per-library scoping
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


@router.post("/discover", response_model=list[DiscoveryResultItem])
@limiter.limit("10/minute")
async def discover_papers(
    request: Request,
    body: DiscoverRequest = Body(...),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    embedder: Embedder | None = Depends(get_embedder),
    user_id: int = Depends(current_user_id_strict),
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
    if body.paper_ids and len(body.paper_ids) > 200:
        raise HTTPException(status_code=400, detail="paper_ids cannot exceed 200 items")
    # Validate that all seed paper IDs exist + are owned by the caller
    async with db_pool.acquire() as conn:
        for paper_id in body.paper_ids:
            await assert_paper_ownership(conn, paper_id, user_id)
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
        user_id=user_id,
    )

    if not results:
        return []

    # Enrich with paper metadata
    paper_ids = [r["paper_id"] for r in results]
    async with db_pool.acquire() as conn:
        # Papers are global; ownership of seed papers is enforced upstream
        # via assert_paper_ownership.
        meta_rows = await conn.fetch(
            """SELECT id, title, authors, url FROM papers
               WHERE id = ANY($1::int[])""",
            paper_ids,
        )
    _ = user_id
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
