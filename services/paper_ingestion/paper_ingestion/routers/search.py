"""Search and relevance-scoring endpoints.

After WS-5B GOD-001 split, this module owns:

* ``POST /api/search``               — multi-source search + DB upsert
* ``POST /api/search-preview``       — multi-source search without DB writes
* ``POST /api/papers/search-hybrid`` — BM25 + semantic RRF hybrid search
* ``POST /api/relevance-score``      — paper/topic relevance score

The feed endpoint moved to ``routers/feed.py``; the discovery endpoints
(``/discover`` and ``/similar/{id}``) moved to ``routers/discovery.py``;
shared helpers and response models live in ``routers/search_helpers.py``.

Helpers and response models are re-exported here for back-compat with tests
that monkeypatch ``paper_ingestion.routers.search`` (e.g.
``test_search_multi_source.py`` imports ``_dedup_papers``, ``_normalize_title``,
``_round_robin_merge`` directly from this module).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from typing import Any

import asyncpg
import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from jarvis_common.auth import current_user_id_or_none
from jarvis_common.db_helpers import assert_paper_ownership

from paper_ingestion.converters import row_to_paper_response
from paper_ingestion.deps import get_db_pool, get_embedder, get_http_client, limiter
from paper_ingestion.ingestion.embedder import Embedder
from paper_ingestion.models import (
    HybridSearchResult,
    PaperCreate,
    PaperResponse,
    RelevanceScoreResponse,
    SearchRequest,
)
from paper_ingestion.routers.search_helpers import (
    PREVIEW_SOURCE_BOOTSTRAP_EXCEPTIONS,
    MultiSourceSearchResponse,
    SearchPreviewLibraryMatch,
    SearchPreviewResponse,
    SearchPreviewResult,
    SearchPreviewSourceError,
    _build_preview_source_error,
    _dedup_papers,
    _library_match_priority,
    _load_local_library_matches,
    _match_preview_result,
    _normalize_author_name,
    _normalize_authors,
    _normalize_title,
    _normalize_url,
    _raise_source_search_error,
    _retry_after_seconds,
    _round_robin_merge,
    _semantic_scholar_api_key_configured,
    _source_display_name,
    _store_preferred_library_match,
    _TitleYearLibraryCandidate,
)
from paper_ingestion.services.pdf_workflow import upsert_paper
from paper_ingestion.services.source_helper import get_source_for_type

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["search"])

# ---------------------------------------------------------------------------
# Back-compat re-exports — keeps test_search_multi_source.py and any future
# `from paper_ingestion.routers.search import ...` callers working without
# the helpers split being a breaking change.
# ---------------------------------------------------------------------------

__all__ = [
    # response models / sentinels
    "MultiSourceSearchResponse",
    "PREVIEW_SOURCE_BOOTSTRAP_EXCEPTIONS",
    "SearchPreviewLibraryMatch",
    "SearchPreviewResponse",
    "SearchPreviewResult",
    "SearchPreviewSourceError",
    # helpers
    "_TitleYearLibraryCandidate",
    "_build_preview_source_error",
    "_dedup_papers",
    "_library_match_priority",
    "_load_local_library_matches",
    "_match_preview_result",
    "_normalize_author_name",
    "_normalize_authors",
    "_normalize_title",
    "_normalize_url",
    "_raise_source_search_error",
    "_retry_after_seconds",
    "_round_robin_merge",
    "_semantic_scholar_api_key_configured",
    "_source_display_name",
    "_store_preferred_library_match",
    # endpoints
    "search_papers",
    "search_papers_preview",
    "search_hybrid",
    "compute_relevance",
    "router",
    "get_source_for_type",
]


# ---------------------------------------------------------------------------
# POST /api/search
# ---------------------------------------------------------------------------


@router.post("/search", response_model=MultiSourceSearchResponse)
@limiter.limit("30/minute")
async def search_papers(
    request: Request,
    body: SearchRequest,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    http_client: httpx.AsyncClient = Depends(get_http_client),
) -> MultiSourceSearchResponse:
    """Search for papers across one or more sources and upsert results into the database.

    Parameters
    ----------
    body : SearchRequest
        Query string, source_types list, and max_results.

    Returns
    -------
    MultiSourceSearchResponse
        Papers found, per-source counts, and any degraded sources.
        Papers are deduplicated across sources.
    """
    if not body.source_types:
        raise HTTPException(status_code=400, detail="At least one source must be selected")
    source_types = body.source_types
    n = len(source_types)
    base_per_source = max(1, body.max_results // n)
    budgets = [base_per_source] * n
    remainder = body.max_results - base_per_source * n
    for i in range(remainder):
        budgets[i] += 1

    # Resolve source instances (skip disabled/unknown without failing the whole request)
    plugins = []
    degraded_sources: list[str] = []
    for st, budget in zip(source_types, budgets):
        try:
            plugin = await get_source_for_type(st, db_pool, http_client, request=request)
            plugins.append((st.value, plugin, budget))
        except HTTPException as e:
            logger.warning("Source %s unavailable for search: %s", st.value, e.detail)
            degraded_sources.append(st.value)

    # Fan-out search across all available sources concurrently
    async def _search_one(
        source_name: str, plugin: Any, budget: int
    ) -> tuple[str, list[PaperCreate]]:
        try:
            papers = await plugin.search(
                body.query,
                budget,
                year_from=body.year_from,
                year_to=body.year_to,
                sort_by=body.sort_by,
                author=body.author,
            )
            return source_name, papers
        except Exception as exc:  # broad: heterogeneous plugins raise different exception types
            logger.warning("Source %s search failed: %s", source_name, exc, exc_info=True)
            return source_name, []

    raw_results = await asyncio.gather(*[_search_one(n, p, b) for n, p, b in plugins])

    # Collect per-source results, track errors
    per_source: dict[str, list[PaperCreate]] = {}
    for source_name, papers in raw_results:
        if not papers and source_name not in degraded_sources:
            # Empty but not an exception — still record counts
            per_source[source_name] = []
        else:
            per_source[source_name] = papers

    # Mark sources that errored (returned empty due to exception)
    for source_name, papers in raw_results:
        if not papers and source_name not in per_source:
            degraded_sources.append(source_name)

    # Merge: date sort → sort merged list; else round-robin interleave
    if body.sort_by == "date":
        all_papers: list[PaperCreate] = []
        for papers in per_source.values():
            all_papers.extend(papers)
        deduped = _dedup_papers(all_papers)
        deduped.sort(key=lambda p: p.published_date or date.min, reverse=True)
    else:
        interleaved = _round_robin_merge(per_source)
        deduped = _dedup_papers(interleaved)

    # Upsert into DB (per original /api/search behavior)
    saved_results: list[PaperResponse] = []
    async with db_pool.acquire() as conn:
        for paper in deduped:
            row = await upsert_paper(conn, paper)
            saved_results.append(row_to_paper_response(row))

    # Build per_source_counts from deduped results
    per_source_counts: dict[str, int] = {}
    for source_name in per_source:
        per_source_counts[source_name] = sum(
            1 for p in deduped if p.source_type.value == source_name
        )

    return MultiSourceSearchResponse(
        results=deduped,
        total=len(deduped),
        per_source_counts=per_source_counts,
        degraded_sources=degraded_sources,
    )


@router.post("/search-preview", response_model=SearchPreviewResponse)
@limiter.limit("30/minute")
async def search_papers_preview(
    request: Request,
    body: SearchRequest,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    http_client: httpx.AsyncClient = Depends(get_http_client),
) -> SearchPreviewResponse:
    """Search papers without saving to DB -- for preview & select flow.

    Supports multi-source fan-out; returns deduplicated results with
    per_source_counts and degraded_sources metadata.
    """
    if not body.source_types:
        raise HTTPException(status_code=400, detail="At least one source must be selected")
    source_types = body.source_types
    n = len(source_types)
    base_per_source = max(1, body.max_results // n)
    budgets = [base_per_source] * n
    remainder = body.max_results - base_per_source * n
    for i in range(remainder):
        budgets[i] += 1

    # Resolve source instances
    plugins = []
    degraded_sources: list[str] = []
    source_errors: dict[str, SearchPreviewSourceError] = {}
    for st, budget in zip(source_types, budgets):
        try:
            plugin = await get_source_for_type(st, db_pool, http_client, request=request)
            plugins.append((st.value, plugin, budget))
        except HTTPException as e:
            logger.warning("Source %s unavailable for preview search: %s", st.value, e.detail)
            source_errors[st.value] = _build_preview_source_error(st.value, e, unavailable=True)
            degraded_sources.append(st.value)
        except PREVIEW_SOURCE_BOOTSTRAP_EXCEPTIONS as exc:
            logger.warning("Source %s unavailable for preview search: %s", st.value, exc)
            source_errors[st.value] = _build_preview_source_error(st.value, exc, unavailable=True)
            degraded_sources.append(st.value)

    if not plugins:
        # All sources failed to load — raise using first source's type for compat
        raise HTTPException(status_code=400, detail="No sources available for search")

    # Fan-out search concurrently
    async def _search_one(
        source_name: str, plugin: Any, budget: int
    ) -> tuple[str, list[PaperCreate], SearchPreviewSourceError | None]:
        try:
            papers = await plugin.search(
                body.query,
                budget,
                year_from=body.year_from,
                year_to=body.year_to,
                sort_by=body.sort_by,
                author=body.author,
            )
            return source_name, papers, None
        except Exception as exc:  # broad: heterogeneous plugins raise different exception types
            logger.warning("Source %s preview search failed: %s", source_name, exc, exc_info=True)
            error = _build_preview_source_error(source_name, exc, plugin=plugin)
            return source_name, [], error

    raw_results = await asyncio.gather(*[_search_one(n, p, b) for n, p, b in plugins])

    per_source: dict[str, list[PaperCreate]] = {}
    for source_name, papers, error in raw_results:
        per_source[source_name] = papers
        if error is not None:
            source_errors[source_name] = error
            degraded_sources.append(source_name)

    # Merge and dedup
    if body.sort_by == "date":
        all_papers: list[PaperCreate] = []
        for papers in per_source.values():
            all_papers.extend(papers)
        deduped = _dedup_papers(all_papers)
        deduped.sort(key=lambda p: p.published_date or date.min, reverse=True)
    else:
        interleaved = _round_robin_merge(per_source)
        deduped = _dedup_papers(interleaved)

    user_id = await current_user_id_or_none(request)
    library_indexes, title_year_candidates = await _load_local_library_matches(db_pool, user_id)
    preview_results = [
        SearchPreviewResult(
            **paper.model_dump(),
            library_match=_match_preview_result(paper, library_indexes, title_year_candidates),
        )
        for paper in deduped
    ]

    per_source_counts: dict[str, int] = {}
    for source_name in per_source:
        per_source_counts[source_name] = sum(
            1 for p in deduped if p.source_type.value == source_name
        )

    return SearchPreviewResponse(
        results=preview_results,
        total=len(deduped),
        per_source_counts=per_source_counts,
        degraded_sources=list(source_errors.keys()),
        source_errors=source_errors,
    )


# ---------------------------------------------------------------------------
# POST /api/papers/search-hybrid
# ---------------------------------------------------------------------------


@router.post("/papers/search-hybrid", response_model=list[HybridSearchResult])
@limiter.limit("30/minute")
async def search_hybrid(
    request: Request,
    body: SearchRequest,
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    embedder: Embedder = Depends(get_embedder),
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
    if embedder.qdrant is None:
        raise HTTPException(
            status_code=503,
            detail="Qdrant unavailable for hybrid search",
        )

    results = await embedder.hybrid_search(body.query, db_pool, limit=body.max_results)
    return results


# ---------------------------------------------------------------------------
# POST /api/relevance-score
# ---------------------------------------------------------------------------


@router.post("/relevance-score", response_model=RelevanceScoreResponse)
@limiter.limit("30/minute")
async def compute_relevance(
    request: Request,
    paper_id: int = Query(...),
    topic_id: int = Query(...),
    db_pool: asyncpg.Pool = Depends(get_db_pool),
    embedder: Embedder | None = Depends(get_embedder),
):
    """Compute and store relevance score between a paper and a topic."""
    user_id = await current_user_id_or_none(request)
    async with db_pool.acquire() as conn:
        await assert_paper_ownership(conn, paper_id, user_id)
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
